import os
import copy
import time
import pickle
from datetime import datetime
import torch
from statistics import mode, mean

from torch.optim import Adam
from torch.nn.modules import loss as nnloss
import torchvision.transforms as transforms
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.tensorboard import SummaryWriter
from torch import nn

from model.networks.inception_siamese_network import (
    get_pretrained_iv3_transforms,
    InceptionSiameseNetwork,
)
from model.networks.light_siamese_network import (
    get_light_siamese_transforms,
    LightSiameseNetwork,
)
from utils import create_logger, readable_float, dynamic_report_key
from model.evaluate import RollingEval

logger = create_logger(__name__)


class QuasiSiameseNetwork(object):
    def __init__(self, args):
        input_size = (args.input_size, args.input_size)

        self.run_name = args.run_name
        self.input_size = input_size
        self.lr = args.learning_rate
        self.output_type = args.output_type

        network_architecture_class = InceptionSiameseNetwork
        network_architecture_transforms = get_pretrained_iv3_transforms
        if args.model_type == "light":
            network_architecture_class = LightSiameseNetwork
            network_architecture_transforms = get_light_siamese_transforms

        # define the loss measure
        if self.output_type == "regression":
            self.criterion = nnloss.MSELoss()
            self.model = network_architecture_class()
        elif self.output_type == "classification":
            self.criterion = nnloss.CrossEntropyLoss()
            self.n_classes = 4  # replace by args
            self.model = network_architecture_class(
                output_type=self.output_type, n_classes=self.n_classes
            )

        self.transforms = {}

        if torch.cuda.device_count() > 1:
            logger.info("Using {} GPUs".format(torch.cuda.device_count()))
            self.model = torch.nn.DataParallel(self.model)

        for s in ("train", "validation", "test", "inference"):
            self.transforms[s] = network_architecture_transforms(s)

        logger.debug("Num params: {}".format(len([_ for _ in self.model.parameters()])))

        self.optimizer = Adam(self.model.parameters(), lr=self.lr)
        # reduces the learning rate when loss plateaus, i.e. doesn't improve
        self.lr_scheduler = ReduceLROnPlateau(
            self.optimizer, factor=0.1, patience=10, min_lr=1e-5, verbose=True
        )
        # creates tracking file for tensorboard
        self.writer = SummaryWriter(args.checkpoint_path)

        self.device = args.device
        self.model_path = args.model_path
        self.prediction_path = args.prediction_path
        self.model_type = args.model_type
        self.is_statistical_model = args.statistical_model
        self.is_neural_model = args.neural_model
        self.log_step = args.log_step

    def get_random_output_values(self, output_shape):
        return torch.rand(output_shape)

    def get_average_output_values(self, output_size, average_label):
        if self.output_type == "regression":
            outputs = torch.ones(output_size) * average_label
        elif self.output_type == "classification":
            average_label_tensor = torch.zeros(self.n_classes)
            average_label_tensor[average_label] = 1
            outputs = average_label_tensor.repeat(output_size[0], 1)
        return outputs

    def calculate_average_label(self, train_set):
        list_of_labels = []
        for _, _, _, label in train_set:
            list_of_labels.extend([label])
        if self.output_type == "regression":
            average_label = mean(list_of_labels)
        elif self.output_type == "classification":
            # use mode as average. mode=number that occurs most often in the list
            average_label = int(mode(list_of_labels))
        return average_label

    def create_prediction_file(self, phase, epoch):
        prediction_file_name = "{}-split_{}-epoch_{:03d}-model_{}-predictions.txt".format(
            self.run_name, phase, epoch, self.model_type
        )
        prediction_file_path = os.path.join(self.prediction_path, prediction_file_name)
        if self.model_type != "probability":
            prediction_file = open(prediction_file_path, "w+")
            prediction_file.write("filename label prediction\n")
            return prediction_file
        else:
            return open(prediction_file_path, "wb")

    def get_outputs_preds(
        self, image1, image2, random_target_shape, average_target_size
    ):
        if self.is_neural_model:
            outputs = self.model(image1, image2).squeeze()
        elif self.model_type == "random":
            output_shape = (
                random_target_shape
                if self.output_type == "regression"
                else (random_target_shape[0], self.n_classes)
            )
            outputs = self.get_random_output_values(output_shape)
        elif self.model_type == "average":
            outputs = self.get_average_output_values(
                average_target_size, self.average_label
            )

        elif self.model_type == "probability":
            outputs = nn.functional.softmax(self.model(image1, image2), dim=1).squeeze()

        outputs = outputs.to(self.device)

        if self.output_type == "classification":
            _, preds = torch.max(outputs, 1)
        else:
            preds = outputs.clamp(0, 1)

        return outputs, preds

    def run_epoch(
        self,
        epoch,
        loader,
        phase="train",
        train_set=None,
        selection_metric="recall_micro",
    ):
        """
        Run one epoch of the model
        Args:
            epoch (int): which epoch this is
            loader: loader object with data
            phase (str): which phase to run epoch for. 'train', 'validation' or 'test'

        Returns:
            epoch_loss (float): loss of this epoch
            epoch_score (float): micro recall of this epoch
        """
        assert phase in ("train", "validation", "test")

        self.model = self.model.to(self.device)

        self.model.eval()
        if phase == "train":
            self.model.train()  # Set model to training mode

        rolling_eval = RollingEval(self.output_type)

        prediction_file = self.create_prediction_file(phase, epoch)

        if self.model_type == "average":
            self.average_label = self.calculate_average_label(train_set)

        if self.model_type == "probability":
            output_probability_list = []

        for idx, (filename, image1, image2, labels) in enumerate(loader, 1):
            image1 = image1.to(self.device)
            image2 = image2.to(self.device)
            if self.output_type == "regression":
                labels = labels.float()
            else:
                labels = labels.long()
            labels = labels.to(self.device)

            if phase == "train":
                # zero the parameter gradients
                self.optimizer.zero_grad()

            with torch.set_grad_enabled(phase == "train"):
                outputs, preds = self.get_outputs_preds(
                    image1, image2, labels.shape, labels.shape
                )
                loss = self.criterion(outputs, labels)

                if phase == "train":
                    loss.backward()
                    self.optimizer.step()

                if self.model_type == "probability":
                    output_probability_list.extend(outputs.tolist())
                else:
                    prediction_file.writelines(
                        [
                            "{} {} {}\n".format(*line)
                            for line in zip(
                                filename,
                                labels.view(-1).tolist(),
                                preds.view(-1).tolist(),
                            )
                        ]
                    )

                batch_loss = loss.item()
                batch_score = rolling_eval.add(labels, preds, batch_loss)

            if idx % self.log_step == 0:
                logger.debug(
                    "Epoch: {:03d} Phase: {:10s} Batch {:04d}/{:04d}: Loss: {:.4f} Accuracy: {:.4f} Correct: {:d} Total: {:d}".format(
                        epoch,
                        phase,
                        idx,
                        len(loader),
                        batch_loss,
                        batch_score[0],
                        batch_score[1],
                        batch_score[2],
                    )
                )
                logger.debug(
                    "Epoch: {:03d} Phase: {:10s} Batch {:04d}/{:04d}: (Micro) Precision: {:.4f} Recall: {:.4f} F1: {:.4f}".format(
                        epoch,
                        phase,
                        idx,
                        len(loader),
                        batch_score[3][0],
                        batch_score[3][1],
                        batch_score[3][2],
                    )
                )
                logger.debug(
                    "Epoch: {:03d} Phase: {:10s} Batch {:04d}/{:04d}: (Macro) Precision: {:.4f} Recall: {:.4f} F1: {:.4f}".format(
                        epoch,
                        phase,
                        idx,
                        len(loader),
                        batch_score[4][0],
                        batch_score[4][1],
                        batch_score[4][2],
                    )
                )
                logger.debug(
                    "Epoch: {:03d} Phase: {:10s} Batch {:04d}/{:04d}: (Weighted) Precision: {:.4f} Recall: {:.4f} F1: {:.4f}".format(
                        epoch,
                        phase,
                        idx,
                        len(loader),
                        batch_score[5][0],
                        batch_score[5][1],
                        batch_score[5][2],
                    )
                )

        epoch_loss = rolling_eval.loss()
        epoch_score = rolling_eval.score()

        second_index_key, first_index_key = selection_metric.split("_")

        first_index = {"micro": 3, "macro": 4, "weighted": 5}
        second_index = {"precision": 0, "recall": 1, "f1": 2}

        epoch_main_metric = epoch_score[first_index[first_index_key]][
            second_index[second_index_key]
        ]

        if self.model_type == "probability":
            pickle.dump(output_probability_list, prediction_file)
        else:
            prediction_file.write(
                "Epoch {:03d} ({}) {}: {:.4f}\n".format(
                    epoch, first_index_key, second_index_key, epoch_main_metric
                )
            )

        prediction_file.close()

        logger.info(
            "Epoch {:03d} Phase: {:10s}: Loss: {:.4f} Accuracy: {:.4f} Correct: {:d} Total: {:d}".format(
                epoch, phase, epoch_loss, epoch_score[0], epoch_score[1], epoch_score[2]
            )
        )
        logger.info(
            "Epoch {:03d} Phase: {:10s}: (Micro) Precision: {:.4f} Recall: {:.4f} F1: {:.4f}".format(
                epoch, phase, epoch_score[3][0], epoch_score[3][1], epoch_score[3][2]
            )
        )
        logger.info(
            "Epoch {:03d} Phase: {:10s}: (Macro) Precision: {:.4f} Recall: {:.4f} F1: {:.4f}".format(
                epoch, phase, epoch_score[4][0], epoch_score[4][1], epoch_score[4][2]
            )
        )
        logger.info(
            "Epoch {:03d} Phase: {:10s}: (Weighted) Precision: {:.4f} Recall: {:.4f} F1: {:.4f}".format(
                epoch, phase, epoch_score[5][0], epoch_score[5][1], epoch_score[5][2]
            )
        )

        return epoch_loss, epoch_main_metric

    def train(self, run_report, datasets, number_of_epochs, selection_metric):
        """
        Train the model
        Args:
            run_report (dict): configuration parameters for reporting training statistics
            datasets: DataSet object with datasets loaded
            number_of_epochs (int): number of epochs to be run

        Returns:
            run_report (dict): configuration parameters for training with training statistics
        """
        train_set, train_loader = datasets.load("train")
        validation_set, validation_loader = datasets.load("validation")

        best_validation_score, best_model_wts = (
            0.0,
            copy.deepcopy(self.model.state_dict()),
        )

        start_time = time.time()
        run_report.train_start_time = (
            datetime.utcnow().replace(microsecond=0).isoformat()
        )
        run_report.train_loss = []
        run_report.train_score = []
        run_report.validation_loss = []
        run_report.validation_score = []

        for epoch in range(1, number_of_epochs + 1):
            # train network
            train_loss, train_score = self.run_epoch(
                epoch, train_loader, phase="train", selection_metric=selection_metric
            )
            run_report.train_loss.append(readable_float(train_loss))
            run_report.train_score.append(readable_float(train_score))

            # eval on validation
            validation_loss, validation_score = self.run_epoch(
                epoch,
                validation_loader,
                phase="validation",
                selection_metric=selection_metric,
            )
            run_report.validation_loss.append(readable_float(validation_loss))
            run_report.validation_score.append(readable_float(validation_score))

            # used for Tensorboard
            self.writer.add_scalar("Train/Loss", train_loss, epoch)
            self.writer.add_scalar("Train/Score", train_score, epoch)
            self.writer.add_scalar("Validation/Loss", validation_loss, epoch)
            self.writer.add_scalar("Validation/Score", validation_score, epoch)

            self.lr_scheduler.step(validation_loss)

            if validation_score > best_validation_score:
                best_validation_score = validation_score
                best_model_wts = copy.deepcopy(self.model.state_dict())

                logger.info(
                    "Epoch {:03d} Checkpoint: Saving to {}".format(
                        epoch, self.model_path
                    )
                )
                torch.save(best_model_wts, self.model_path)

        time_elapsed = time.time() - start_time
        run_report.train_end_time = datetime.utcnow().replace(microsecond=0).isoformat()

        run_report.train_duration = "{:.0f}m {:.0f}s".format(
            time_elapsed // 60, time_elapsed % 60
        )

        logger.info("Training complete in {}".format(run_report.train_duration))

        logger.info("Best validation score: {:4f}.".format(best_validation_score))
        return run_report

    def test(self, run_report, datasets):
        """
        Test the model
        Args:
            run_report (dict): configuration parameters for reporting testing statistics
            datasets: DataSet object with datasets loaded

        Returns:
            run_report (dict): configuration parameters for testing with testing statistics
        """
        if self.is_statistical_model:
            train_set, _ = datasets.load("train")
        else:
            self.model.load_state_dict(
                torch.load(self.model_path, map_location=self.device)
            )
        test_set, test_loader = datasets.load("test")
        start_time = time.time()
        run_report[
            dynamic_report_key(
                "test_start_time", self.model_type, self.is_statistical_model
            )
        ] = (datetime.utcnow().replace(microsecond=0).isoformat())
        test_loss, test_score = self.run_epoch(
            1,
            test_loader,
            phase="test",
            train_set=train_set if self.is_statistical_model else None,
        )
        run_report[
            dynamic_report_key("test_loss", self.model_type, self.is_statistical_model)
        ] = readable_float(test_loss)
        run_report[
            dynamic_report_key("test_score", self.model_type, self.is_statistical_model)
        ] = readable_float(test_score)
        time_elapsed = time.time() - start_time
        run_report[
            dynamic_report_key(
                "test_end_time", self.model_type, self.is_statistical_model
            )
        ] = (datetime.utcnow().replace(microsecond=0).isoformat())

        run_report[
            dynamic_report_key(
                "test_duration", self.model_type, self.is_statistical_model
            )
        ] = "{:.0f}m {:.0f}s".format(time_elapsed // 60, time_elapsed % 60)

        run_report[
            dynamic_report_key("test", self.model_type, self.is_statistical_model)
        ] = True

        logger.info(
            "Testing complete in {}".format(
                run_report[
                    dynamic_report_key(
                        "test_duration", self.model_type, self.is_statistical_model
                    )
                ]
            )
        )
        return run_report

    def inference(self, datasets):
        """
        Uses the model for inference
        Args:
            datasets: DataSet object with datasets loaded
        """
        if self.is_statistical_model:
            train_set, _ = datasets.load("train")
        else:
            self.model.load_state_dict(
                torch.load(self.model_path, map_location=self.device)
            )
        inference_set, inference_loader = datasets.load("inference")
        start_time = time.time()

        self.model = self.model.to(self.device)

        self.model.eval()

        prediction_file = self.create_prediction_file("inference", 1)
        prediction_file.write("filename prediction\n")

        if self.model_type == "average":
            self.average_label = self.calculate_average_label(train_set)

        for idx, (filename, image1, image2) in enumerate(inference_loader, 1):
            image1 = image1.to(self.device)
            image2 = image2.to(self.device)

            outputs, preds = self.get_outputs_preds(
                image1, image2, image1.shape, [image1.shape[0]]
            )

            prediction_file.writelines(
                [
                    "{} {}\n".format(*line)
                    for line in zip(filename, preds.view(-1).tolist())
                ]
            )

        time_elapsed = time.time() - start_time

        logger.info(
            "Inference complete in {:.0f}m {:.0f}s".format(
                time_elapsed // 60, time_elapsed % 60
            )
        )
