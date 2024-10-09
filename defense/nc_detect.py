
max_analyzing = 0   # for debug
dataset_num_ratio = 0

import argparse
import os, sys
import numpy as np
import torch
import torch.nn as nn
import cv2

sys.path.append('../')
sys.path.append(os.getcwd())

from pprint import pformat
import yaml
import logging
import time
from defense.base import defense
from matplotlib import image as mlt
from PIL import Image
import torchvision

from utils.aggregate_block.train_settings_generate import argparser_criterion, argparser_opt_scheduler
from utils.trainer_cls import Metric_Aggregator, PureCleanModelTrainer
from utils.choose_index import choose_index
from utils.aggregate_block.fix_random import fix_random
from utils.aggregate_block.model_trainer_generate import generate_cls_model
from utils.log_assist import get_git_info
from utils.aggregate_block.dataset_and_transform_generate import get_input_shape, get_num_classes, get_transform
from utils.save_load_attack import load_attack_result, save_defense_result
from utils.bd_dataset_v2 import prepro_cls_DatasetBD_v2, xy_iter


class Normalize:

    def __init__(self, opt, expected_values, variance):
        self.n_channels = opt.input_channel
        self.expected_values = expected_values
        self.variance = variance
        assert self.n_channels == len(self.expected_values)

    def __call__(self, x):
        x_clone = x.clone()
        for channel in range(self.n_channels):
            x_clone[:, channel] = (x[:, channel] - self.expected_values[channel]) / self.variance[channel]
        return x_clone


class Denormalize:

    def __init__(self, opt, expected_values, variance):
        self.n_channels = opt.input_channel
        self.expected_values = expected_values
        self.variance = variance
        assert self.n_channels == len(self.expected_values)

    def __call__(self, x):
        x_clone = x.clone()
        for channel in range(self.n_channels):
            x_clone[:, channel] = x[:, channel] * self.variance[channel] + self.expected_values[channel]
        return x_clone


class RegressionModel(nn.Module):
    def __init__(self, opt, init_mask, init_pattern, result):
        self._EPSILON = opt.EPSILON
        super(RegressionModel, self).__init__()
        # 初始mask和trigger
        self.mask_tanh = nn.Parameter(torch.tensor(init_mask))
        self.pattern_tanh = nn.Parameter(torch.tensor(init_pattern))

        self.result = result
        self.classifier = self._get_classifier(opt)
        self.normalizer = self._get_normalize(opt)
        self.denormalizer = self._get_denormalize(opt)

    def forward(self, x):
        # 每次得到的mask和trigger都不变的吗？没有优化吗？
        mask = self.get_raw_mask()
        pattern = self.get_raw_pattern()
        if self.normalizer:
            pattern = self.normalizer(self.get_raw_pattern())

        x = (1 - mask) * x + mask * pattern
        return self.classifier(x)

    def get_raw_mask(self):
        mask = nn.Tanh()(self.mask_tanh)    # nn.Tanh()只是激活函数，没有可学习的参数
        return mask / (2 + self._EPSILON) + 0.5

    def get_raw_pattern(self):
        pattern = nn.Tanh()(self.pattern_tanh)
        return pattern / (2 + self._EPSILON) + 0.5

    def _get_classifier(self, opt):
        # 分类器：和attack中的model一样
        classifier = generate_cls_model(args.model, args.num_classes)
        classifier.load_state_dict(self.result['model'])
        classifier.to(args.device)

        for param in classifier.parameters():
            param.requires_grad = False
        classifier.eval()
        return classifier.to(opt.device)

    def _get_denormalize(self, opt):
        if opt.dataset == "cifar10" or opt.dataset == "cifar100":
            denormalizer = Denormalize(opt, [0.4914, 0.4822, 0.4465], [0.247, 0.243, 0.261])
        elif opt.dataset == "mnist":
            denormalizer = Denormalize(opt, [0.5], [0.5])
        elif opt.dataset == "gtsrb" or opt.dataset == "celeba":
            denormalizer = None
        elif opt.dataset == 'tiny':
            denormalizer = Denormalize(opt, [0.4802, 0.4481, 0.3975], [0.2302, 0.2265, 0.2262])
        else:
            raise Exception("Invalid dataset")
        return denormalizer

    def _get_normalize(self, opt):
        if opt.dataset == "cifar10" or opt.dataset == "cifar100":
            normalizer = Normalize(opt, [0.4914, 0.4822, 0.4465], [0.247, 0.243, 0.261])
        elif opt.dataset == "mnist":
            normalizer = Normalize(opt, [0.5], [0.5])
        elif opt.dataset == "gtsrb" or opt.dataset == "celeba":
            normalizer = None
        elif opt.dataset == 'tiny':
            normalizer = Normalize(opt, [0.4802, 0.4481, 0.3975], [0.2302, 0.2265, 0.2262])
        else:
            raise Exception("Invalid dataset")
        return normalizer


class Recorder:
    def __init__(self, opt):
        super().__init__()

        # Best optimization results
        self.mask_best = None
        self.pattern_best = None
        self.reg_best = float("inf")

        # Logs and counters for adjusting balance cost
        self.logs = []
        self.cost_set_counter = 0
        self.cost_up_counter = 0
        self.cost_down_counter = 0
        self.cost_up_flag = False
        self.cost_down_flag = False

        # Counter for early stop
        self.early_stop_counter = 0
        self.early_stop_reg_best = self.reg_best

        # Cost
        self.cost = opt.init_cost   # self.cost是mask reg的放缩程度
        self.cost_multiplier_up = opt.cost_multiplier
        self.cost_multiplier_down = opt.cost_multiplier ** 1.5

    def reset_state(self, opt):
        self.cost = opt.init_cost
        self.cost_up_counter = 0
        self.cost_down_counter = 0
        self.cost_up_flag = False
        self.cost_down_flag = False
        print("Initialize cost to {:f}".format(self.cost))

    def save_result_to_dir(self, opt):

        result_dir = (os.getcwd() + '/' + f'{opt.log}')
        if not os.path.exists(result_dir):
            os.makedirs(result_dir)
        result_dir = os.path.join(result_dir, str(opt.target_label))
        if not os.path.exists(result_dir):
            os.makedirs(result_dir)

        pattern_best = self.pattern_best
        mask_best = self.mask_best
        trigger = pattern_best * mask_best

        path_mask = os.path.join(result_dir, "mask.png")
        path_pattern = os.path.join(result_dir, "pattern.png")
        path_trigger = os.path.join(result_dir, "trigger.png")

        torchvision.utils.save_image(mask_best, path_mask, normalize=True)
        torchvision.utils.save_image(pattern_best, path_pattern, normalize=True)
        torchvision.utils.save_image(trigger, path_trigger, normalize=True)


def train_mask(args, result, trainloader, init_mask, init_pattern):
    ### 为什么要优化RegressionModel的参数？好像并没有对mask和trigger进行优化？A:有，mask是可学习的张量

    # Build regression model，里面有mask和trigger作为两个参数，通过Tanh得到
    regression_model = RegressionModel(args, init_mask, init_pattern, result).to(args.device)

    # Set optimizer，对regression_model中的classifier进行更新
    optimizerR = torch.optim.Adam(regression_model.parameters(), lr=args.mask_lr, betas=(0.5, 0.9))

    # Set recorder (for recording best result) ，Recorder会保存最优的mask和trigger
    recorder = Recorder(args)

    for epoch in range(args.nc_epoch):
        # early_stop = train_step(regression_model, optimizerR, test_dataloader, recorder, epoch, opt)
        early_stop = train_step(regression_model, optimizerR, trainloader, recorder, epoch, args)
        if early_stop:
            break

    # Save result to dir
    recorder.save_result_to_dir(args)

    return recorder, args


def train_step(regression_model, optimizerR, dataloader, recorder, epoch, opt):
    # print("Epoch {} - Label: {} | {} - {}:".format(epoch, opt.target_label, opt.dataset, opt.attack_mode))
    # Set losses
    cross_entropy = nn.CrossEntropyLoss()
    total_pred = 0
    true_pred = 0

    # Record loss for all mini-batches
    loss_ce_list = []
    loss_reg_list = []
    loss_list = []
    loss_acc_list = []

    # Set inner early stop flag
    inner_early_stop_flag = False
    # print(f"length of dataloader: {len(dataloader.dataset)}")
    for batch_idx, (inputs, labels, *other_info) in enumerate(dataloader):
        # print(f"batch_idx: {batch_idx}")
        # Forwarding and update model
        optimizerR.zero_grad()

        inputs = inputs.to(opt.device)
        sample_num = inputs.shape[0]
        # assert sample_num != 0
        total_pred += sample_num
        target_labels = torch.ones((sample_num), dtype=torch.int64).to(opt.device) * opt.target_label
        predictions = regression_model(inputs)

        loss_ce = cross_entropy(predictions, target_labels)
        loss_reg = torch.norm(regression_model.get_raw_mask(), opt.use_norm)
        total_loss = loss_ce + recorder.cost * loss_reg
        total_loss.backward()
        optimizerR.step()

        # Record minibatch information to list
        minibatch_accuracy = torch.sum(torch.argmax(predictions, dim=1) == target_labels).detach() * 100.0 / sample_num
        loss_ce_list.append(loss_ce.detach())
        loss_reg_list.append(loss_reg.detach())
        loss_list.append(total_loss.detach())
        loss_acc_list.append(minibatch_accuracy)

        true_pred += torch.sum(torch.argmax(predictions, dim=1) == target_labels).detach()
        # progress_bar(batch_idx, len(dataloader))

    # print(f"length of loss_ce_list: {len(loss_ce_list)}")
    loss_ce_list = torch.stack(loss_ce_list)
    loss_reg_list = torch.stack(loss_reg_list)
    loss_list = torch.stack(loss_list)
    loss_acc_list = torch.stack(loss_acc_list)

    avg_loss_ce = torch.mean(loss_ce_list)
    avg_loss_reg = torch.mean(loss_reg_list)
    avg_loss = torch.mean(loss_list)
    avg_loss_acc = torch.mean(loss_acc_list)

    # Check to save best mask or not
    if avg_loss_acc >= opt.atk_succ_threshold and avg_loss_reg < recorder.reg_best:
        # 如果loss更高了(攻击效果更好)并且reg更小，就说明当前的mask和trigger比以往都要好，于是更新全局变量mask_best和pattern_best。
        recorder.mask_best = regression_model.get_raw_mask().detach()
        recorder.pattern_best = regression_model.get_raw_pattern().detach()
        recorder.reg_best = avg_loss_reg
        recorder.save_result_to_dir(opt)
        # print(" Updated !!!")

    # Show information
    # print(
    #     "  Result: Accuracy: {:.3f} | Cross Entropy Loss: {:.6f} | Reg Loss: {:.6f} | Reg best: {:.6f}".format(
    #         true_pred * 100.0 / total_pred, avg_loss_ce, avg_loss_reg, recorder.reg_best
    #     )
    # )

    # Check early stop
    if opt.early_stop:
        if recorder.reg_best < float("inf"):
            if recorder.reg_best >= opt.early_stop_threshold * recorder.early_stop_reg_best:
                recorder.early_stop_counter += 1
            else:
                recorder.early_stop_counter = 0

        recorder.early_stop_reg_best = min(recorder.early_stop_reg_best, recorder.reg_best)

        if (
                recorder.cost_down_flag
                and recorder.cost_up_flag
                and recorder.early_stop_counter >= opt.early_stop_patience
        ):
            print("Early_stop !!!")
            inner_early_stop_flag = True

    if not inner_early_stop_flag:
        # Check cost modification
        if recorder.cost == 0 and avg_loss_acc >= opt.atk_succ_threshold:
            recorder.cost_set_counter += 1
            if recorder.cost_set_counter >= opt.patience:
                recorder.reset_state(opt)
        else:
            recorder.cost_set_counter = 0

        if avg_loss_acc >= opt.atk_succ_threshold:
            recorder.cost_up_counter += 1
            recorder.cost_down_counter = 0
        else:
            recorder.cost_up_counter = 0
            recorder.cost_down_counter += 1

        if recorder.cost_up_counter >= opt.patience:
            recorder.cost_up_counter = 0
            print("Up cost from {} to {}".format(recorder.cost, recorder.cost * recorder.cost_multiplier_up))
            recorder.cost *= recorder.cost_multiplier_up
            recorder.cost_up_flag = True

        elif recorder.cost_down_counter >= opt.patience:
            recorder.cost_down_counter = 0
            print("Down cost from {} to {}".format(recorder.cost, recorder.cost / recorder.cost_multiplier_down))
            recorder.cost /= recorder.cost_multiplier_down
            recorder.cost_down_flag = True

        # Save the final version
        if recorder.mask_best is None:
            recorder.mask_best = regression_model.get_raw_mask().detach()
            recorder.pattern_best = regression_model.get_raw_pattern().detach()

    del predictions
    torch.cuda.empty_cache()

    return inner_early_stop_flag


def outlier_detection(l1_norm_list, idx_mapping, opt):
    print("-" * 30)
    print("Determining whether model is backdoor")
    consistency_constant = 1.4826
    median = torch.median(l1_norm_list)
    mad = consistency_constant * torch.median(torch.abs(l1_norm_list - median))
    min_mad = torch.abs(torch.min(l1_norm_list) - median) / mad

    print("Median: {}, MAD: {}".format(median, mad))
    print(f"Anomaly index: {min_mad}, if min_mad < 2 that means Not a backdoor model")

    # if min_mad < 2:
    #     print("Not a backdoor model")
    # else:
    #     print("This is a backdoor model")
    #
    # if opt.to_file:
    #     # result_path = os.path.join(opt.result, opt.saving_prefix, opt.dataset)
    #     # output_path = os.path.join(
    #     #     result_path, "{}_{}_output.txt".format(opt.attack_mode, opt.dataset, opt.attack_mode)
    #     # )
    #     output_path = opt.output_path
    #     with open(output_path, "a+") as f:
    #         f.write(
    #             str(median.cpu().numpy()) + ", " + str(mad.cpu().numpy()) + ", " + str(min_mad.cpu().numpy()) + "\n"
    #         )
    #         l1_norm_list_to_save = [str(value) for value in l1_norm_list.cpu().numpy()]
    #         f.write(", ".join(l1_norm_list_to_save) + "\n")
    #
    # flag_list = []
    # for y_label in idx_mapping:
    #     if l1_norm_list[idx_mapping[y_label]] > median:
    #         continue
    #     if torch.abs(l1_norm_list[idx_mapping[y_label]] - median) / mad > 2:
    #         flag_list.append((y_label, l1_norm_list[idx_mapping[y_label]]))
    #
    # if len(flag_list) > 0:
    #     flag_list = sorted(flag_list, key=lambda x: x[1])
    #
    # logging.info(
    #     "Flagged label list: {}".format(",".join(["{}: {}".format(y_label, l_norm) for y_label, l_norm in flag_list]))
    # )
    #
    # return flag_list


class nc(defense):

    def __init__(self, args):
        with open(args.yaml_path, 'r') as f:
            defaults = yaml.safe_load(f)

        defaults.update({k: v for k, v in args.__dict__.items() if v is not None})

        args.__dict__ = defaults

        args.terminal_info = sys.argv

        args.num_classes = get_num_classes(args.dataset)
        args.input_height, args.input_width, args.input_channel = get_input_shape(args.dataset)
        args.img_size = (args.input_height, args.input_width, args.input_channel)
        args.dataset_path = f"{args.dataset_path}/{args.dataset}"

        self.args = args

        if 'result_file' in args.__dict__:
            if args.result_file is not None:
                self.set_result(args.result_file)

    def add_arguments(parser):
        parser.add_argument('--device', type=str, help='cuda, cpu')
        parser.add_argument("-pm", "--pin_memory", type=lambda x: str(x) in ['True', 'true', '1'],
                            help="dataloader pin_memory")
        parser.add_argument("-nb", "--non_blocking", type=lambda x: str(x) in ['True', 'true', '1'],
                            help=".to(), set the non_blocking = ?")
        parser.add_argument("-pf", '--prefetch', type=lambda x: str(x) in ['True', 'true', '1'], help='use prefetch')
        parser.add_argument('--amp', type=lambda x: str(x) in ['True', 'true', '1'])

        parser.add_argument('--checkpoint_load', type=str, help='the location of load model')
        parser.add_argument('--checkpoint_save', type=str, help='the location of checkpoint where model is saved')
        parser.add_argument('--log', type=str, help='the location of log')
        parser.add_argument("--dataset_path", type=str, help='the location of data')
        parser.add_argument('--dataset', type=str, help='mnist, cifar10, cifar100, gtrsb, tiny')
        parser.add_argument('--attack_file', type=str, help='the location of attack result')
        parser.add_argument('--defense_file', type=str, help='the location of defense result')

        parser.add_argument('--epochs', type=int)
        parser.add_argument('--batch_size', type=int)
        parser.add_argument("--num_workers", type=int)
        parser.add_argument("--max_analyzing", type=int)
        parser.add_argument("--dataset_num_ratio", type=float)
        parser.add_argument('--lr', type=float)
        parser.add_argument('--lr_scheduler', type=str, help='the scheduler of lr')
        parser.add_argument('--steplr_stepsize', type=int)
        parser.add_argument('--steplr_gamma', type=float)
        parser.add_argument('--steplr_milestones', type=list)
        parser.add_argument('--model', type=str, help='resnet18')

        parser.add_argument('--client_optimizer', type=int)
        parser.add_argument('--sgd_momentum', type=float)
        parser.add_argument('--wd', type=float, help='weight decay of sgd')
        parser.add_argument('--frequency_save', type=int,
                            help=' frequency_save, 0 is never')

        parser.add_argument('--random_seed', type=int, help='random seed')
        parser.add_argument('--yaml_path', type=str, default="./config/defense/nc/config.yaml", help='the path of yaml')

        # set the parameter for the nc defense
        parser.add_argument('--ratio', type=float, help='ratio of training data')
        parser.add_argument('--cleaning_ratio', type=float, help='ratio of cleaning data')
        parser.add_argument('--unlearning_ratio', type=float, help='ratio of unlearning data')
        parser.add_argument('--nc_epoch', type=int, help='the epoch for neural cleanse')

        parser.add_argument('--index', type=str, help='index of clean data')

    def set_result(self, attack_file, defense_file):
        self.attack_file = attack_file
        save_path = 'record/defense/nc/'
        if not (os.path.exists(save_path)):
            os.makedirs(save_path)
        # assert(os.path.exists(save_path))
        self.args.save_path = save_path
        if self.args.checkpoint_save is None:
            self.args.checkpoint_save = save_path + 'checkpoint/'
            if not (os.path.exists(self.args.checkpoint_save)):
                os.makedirs(self.args.checkpoint_save)
        if self.args.log is None:
            self.args.log = save_path + 'log/'
            if not (os.path.exists(self.args.log)):
                os.makedirs(self.args.log)
        self.result = load_attack_result(attack_file, defense_file)

    def set_trainer(self, model):
        self.trainer = PureCleanModelTrainer(
            model,
        )

    def set_logger(self):
        args = self.args
        logFormatter = logging.Formatter(
            fmt='%(asctime)s [%(levelname)-8s] [%(filename)s:%(lineno)d] %(message)s',
            datefmt='%Y-%m-%d:%H:%M:%S',
        )
        logger = logging.getLogger()

        fileHandler = logging.FileHandler(
            args.log + '/' + time.strftime("%Y_%m_%d_%H_%M_%S", time.localtime()) + '.log')
        fileHandler.setFormatter(logFormatter)
        logger.addHandler(fileHandler)

        consoleHandler = logging.StreamHandler()
        consoleHandler.setFormatter(logFormatter)
        logger.addHandler(consoleHandler)

        logger.setLevel(logging.INFO)
        logging.info(pformat(args.__dict__))

        try:
            logging.info(pformat(get_git_info()))
        except:
            logging.info('Getting git info fails.')

    def set_devices(self):
        # self.device = torch.device(
        #     (
        #         f"cuda:{[int(i) for i in self.args.device[5:].split(',')][0]}" if "," in self.args.device else self.args.device
        #         # since DataParallel only allow .to("cuda")
        #     ) if torch.cuda.is_available() else "cpu"
        # )
        self.device = self.args.device

    def mitigation(self):
        self.set_devices()
        fix_random(self.args.random_seed)
        args = self.args
        result = self.result

        # Prepare model, optimizer, scheduler
        model = generate_cls_model(self.args.model, self.args.num_classes)
        model.load_state_dict(self.result['model'])
        if "," in self.device:
            model = torch.nn.DataParallel(
                model,
                device_ids=[int(i) for i in self.args.device[5:].split(",")]  # eg. "cuda:2,3,7" -> [2,3,7]
            )
            self.args.device = f'cuda:{model.device_ids[0]}'
            model.to(self.args.device)
        else:
            model.to(self.args.device)
        optimizer, scheduler = argparser_opt_scheduler(model, self.args)
        # criterion = nn.CrossEntropyLoss()
        self.set_trainer(model)
        criterion = argparser_criterion(args)

        # 准备数据集 和 dataloader
        # 只用一部分的数据集进行训练，不然每个epoch太慢了。
        def get_ramdomsplit_dataset(train_dataset, bs=256, ratio=0.2):
            from torch.utils.data import DataLoader, SubsetRandomSampler
            ratio = min(ratio, 1)
            # 为每个类选择相同数量的样本
            class_indices = {}
            for i in range(len(train_dataset)):  # 把所有数据的idx根据label分好类
                items = train_dataset[i]
                label = items[1]
                if label not in class_indices:
                    class_indices[label] = []
                class_indices[label].append(i)
            subset_indices = []
            totalnum = 0
            for indices_list in class_indices.values():
                sub_length = len(indices_list)
                assert sub_length != 0
                totalnum += sub_length
            label_num = len(class_indices)
            num_samples_per_label = round(totalnum * ratio / label_num)
            print(
                f" --- totalnum: {totalnum}, label_num: {label_num}, num_samples_per_label: {num_samples_per_label} --- ")
            for indices in class_indices.values():
                subset_indices.extend(indices[:num_samples_per_label])
            # 创建 SubsetRandomSampler 对象来读取子集数据集
            subset_sampler = SubsetRandomSampler(subset_indices)
            subset_dataloader = DataLoader(train_dataset, batch_size=bs,
                                           num_workers=self.args.num_workers, shuffle=False,
                                           pin_memory=args.pin_memory, sampler=subset_sampler, drop_last=False)
            return subset_dataloader


        train_tran = get_transform(self.args.dataset, *([self.args.input_height, self.args.input_width]), train=True)
        clean_dataset = prepro_cls_DatasetBD_v2(self.result['clean_train'].wrapped_dataset)
        data_all_length = len(clean_dataset)
        ran_idx = choose_index(self.args, data_all_length)
        log_index = self.args.log + 'index.txt'
        np.savetxt(log_index, ran_idx, fmt='%d')
        clean_dataset.subset(ran_idx)
        data_set_without_tran = clean_dataset
        data_set_o = self.result['clean_train']
        data_set_o.wrapped_dataset = data_set_without_tran
        data_set_o.wrap_img_transform = train_tran

        print(f"cutting train dataset to ratio {dataset_num_ratio * 100:.2f}%")
        data_loader = get_ramdomsplit_dataset(data_set_o, self.args.batch_size, dataset_num_ratio)

        # data_loader = torch.utils.data.DataLoader(data_set_o, batch_size=self.args.batch_size,
        #                                           num_workers=self.args.num_workers, shuffle=True,
        #                                           pin_memory=args.pin_memory)
        trainloader = data_loader
        print(f"length: {len(trainloader.dataset)}")

        # wanet检测原理: 每个标签都分别当作一个target，优化出一个mask和trigger，然后用L1计算mask和trigger的大小，
        # 如果有特别小的，就说明该模型是bd model.

        # a. initialize the model and trigger
        result_path = os.getcwd() + '/' + f'{args.save_path}/nc/trigger/'
        if not os.path.exists(result_path):
            os.makedirs(result_path)
        args.output_path = result_path + "{}_output_clean.txt".format(args.dataset)
        if args.to_file:
            with open(args.output_path, "w+") as f:
                f.write("Output for cleanse:  - {}".format(args.dataset) + "\n")

        init_mask = np.ones((1, args.input_height, args.input_width)).astype(np.float32)    # (1,h,w)
        init_pattern = np.ones((args.input_channel, args.input_height, args.input_width)).astype(np.float32)    # (c,h,w),trigger的shape要和img一样。

        flag = 0
        print(f" == args.n_times_test: {args.n_times_test}")
        print(f" == args.num_classes: {args.num_classes}")
        for test in range(args.n_times_test):
            # b. train triggers according to different target labels
            print("Test {}:".format(test))
            logging.info("Test {}:".format(test))
            if args.to_file:
                with open(args.output_path, "a+") as f:
                    f.write("-" * 30 + "\n")
                    f.write("Test {}:".format(str(test)) + "\n")

            masks = []
            idx_mapping = {}

            for target_label in range(min(max_analyzing, args.num_classes)):    # 对每个标签都优化出一个mask和trigger
                print("----------------- Analyzing label: {} -----------------".format(target_label))
                logging.info("----------------- Analyzing label: {} -----------------".format(target_label))
                args.target_label = target_label
                recorder, args = train_mask(args, result, trainloader, init_mask, init_pattern)

                mask = recorder.mask_best
                masks.append(mask)
                reg = torch.norm(mask, p=args.use_norm)
                logging.info(f'The regularization of mask for target label {target_label} is {reg}')
                idx_mapping[target_label] = len(masks) - 1

            # c. Determine whether the trained reverse trigger is a real backdoor trigger
            l1_norm_list = torch.stack([torch.norm(m, p=args.use_norm) for m in masks])
            logging.info("{} labels found".format(len(l1_norm_list)))
            logging.info("Norm values: {}".format(l1_norm_list))
            # flag_list = outlier_detection(l1_norm_list, idx_mapping, args)  # 离群值检测
            outlier_detection(l1_norm_list, idx_mapping, args)  # 离群值检测
            # if len(flag_list) != 0:
            #     flag = 1

    def defense(self, attack_file, defense_file):
        global max_analyzing
        global dataset_num_ratio
        self.set_result(attack_file, defense_file)
        self.set_logger()
        # result = self.mitigation()
        self.mitigation()
        return None


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description=sys.argv[0])
    nc.add_arguments(parser)
    args = parser.parse_args()
    max_analyzing = args.max_analyzing  # for debug
    dataset_num_ratio = args.dataset_num_ratio
    nc_method = nc(args)
    if "defense_file" not in args.__dict__:
        args.defense_file = None  
    result = nc_method.defense(args.attack_file, args.defense_file)
