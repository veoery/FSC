import os
import sys
import time
import logging
from math import ceil

import torch
from torch import optim
from torch.utils.data import DataLoader

from datasets.crowd_dmap import Crowd
from models.chsnet import CHSNet
from losses.losses import CHSLoss
from utils.trainer import Trainer
from utils.helper import Save_Handle, AverageMeter

import numpy as np
from tqdm import tqdm
import wandb

# sys.path.append(os.path.join(os.path.dirname(__file__), ".."))


def train_collate(batch):
    transposed_batch = list(zip(*batch))
    images = torch.stack(transposed_batch[0], 0)
    dmaps = torch.stack(transposed_batch[1], 0)
    return images, dmaps


class CHSNetTrainer(Trainer):
    def setup(self):
        """initial the datasets, model, loss and optimizer"""
        args = self.args
        if torch.cuda.is_available():
            self.device = torch.device("cuda")
        else:
            raise Exception("gpu is not available")

        train_datasets = Crowd(os.path.join(args.data_dir, 'train'),
                               args.crop_size,
                               args.downsample_ratio,
                               args.is_gray, method='train')
        train_dataloaders = DataLoader(train_datasets,
                                       collate_fn=train_collate,
                                       batch_size=args.batch_size,
                                       #shuffle=True,
                                       shuffle=False,
                                       num_workers=args.num_workers,
                                       pin_memory=True)
        val_datasets = Crowd(os.path.join(args.data_dir, 'test'), 512, 8, is_gray=False, method='val')
        val_dataloaders = torch.utils.data.DataLoader(val_datasets, 1, shuffle=False,
                                                       num_workers=args.num_workers, pin_memory=True)
        self.dataloaders = {'train': train_dataloaders, 'val': val_dataloaders}

        self.model = CHSNet(dcsize=args.dcsize)
        self.model.to(self.device)

        self.optimizer = optim.Adam(self.model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

        self.criterion = CHSLoss(size=args.dcsize, max_noisy_ratio=args.max_noisy_ratio, max_weight_ratio=args.max_weight_ratio)

        self.save_list = Save_Handle(max_num=args.max_model_num)
        self.best_mae = np.inf
        self.best_mse = np.inf
        self.best_mae_at = 0
        self.best_count = 0

        self.start_epoch = 0
        if args.resume:
            suf = args.resume.rsplit('.', 1)[-1]
            if suf == 'tar':
                checkpoint = torch.load(args.resume, self.device)
                self.model.load_state_dict(checkpoint['model_state_dict'])
                self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
                self.start_epoch = checkpoint['epoch'] + 1
                self.best_mae = checkpoint['best_mae']
                self.best_mse = checkpoint['best_mse']
                self.best_mae_at = checkpoint['best_mae_at']
            elif suf == 'pth':
                self.model.load_state_dict(torch.load(args.resume, self.device))

        if args.scheduler == 'step':
            self.scheduler = optim.lr_scheduler.StepLR(self.optimizer, step_size=args.step, gamma=args.gamma, last_epoch=self.start_epoch-1)
        elif args.scheduler == 'cosine':
            self.scheduler = optim.lr_scheduler.CosineAnnealingLR(self.optimizer, T_max=args.t_max, eta_min=args.eta_min, last_epoch=self.start_epoch-1)

    def train(self):
        args = self.args
        self.epoch = None
        # self.val_epoch()
        for epoch in range(self.start_epoch, args.max_epoch):
            logging.info('-' * 5 + 'Epoch {}/{}'.format(epoch, args.max_epoch - 1) + '-' * 5)
            logging.info('args.data_dir{}'.format(args.data_dir))
            self.epoch = epoch
            self.train_epoch()
            self.scheduler.step()
            if epoch >= args.val_start and (epoch % args.val_epoch == 0 or epoch == args.max_epoch - 1):
                self.val_epoch()

    def train_epoch(self):
        epoch_loss = AverageMeter()
        epoch_mae = AverageMeter()
        epoch_mse = AverageMeter()
        epoch_start = time.time()
        self.model.train()
        logging.info('OK')
        # Iterate over data.
        for inputs, targets in tqdm(self.dataloaders['train']):
            inputs = inputs.to(self.device)
            targets = targets.to(self.device) * self.args.log_param
            
            with torch.set_grad_enabled(True):

                dmap_conv, dmap_tran = self.model(inputs)
                loss = self.criterion(dmap_conv, dmap_tran, targets, self.epoch/self.args.max_epoch)
                dmap = (dmap_conv + dmap_tran) / 2.0

                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

                N = inputs.size(0)
                pre_count = torch.sum(dmap.view(N, -1), dim=1).detach().cpu().numpy()
                gd_count = torch.sum(targets.view(N, -1), dim=1).detach().cpu().numpy()
                res = pre_count - gd_count
                logging.info('res: {}'
                     .format(res))
                epoch_loss.update(loss.item(), N)
                epoch_mse.update(np.mean(res * res), N)
                epoch_mae.update(np.mean(abs(res)), N)

        logging.info('Epoch {} Train, Loss: {:.2f}, MSE: {:.2f} MAE: {:.2f}, Cost {:.1f} sec'
                     .format(self.epoch, epoch_loss.get_avg(), np.sqrt(epoch_mse.get_avg()), epoch_mae.get_avg(),
                             time.time() - epoch_start))
        wandb.log({'Train/loss': epoch_loss.get_avg(),
                   'Train/lr': self.scheduler.get_last_lr()[0],
                   'Train/epoch_mae': epoch_mae.get_avg()}, step=self.epoch)

        model_state_dic = self.model.state_dict()
        save_path = os.path.join(self.save_dir, '{}_ckpt.tar'.format(self.epoch))
        torch.save({
            'epoch': self.epoch,
            'optimizer_state_dict': self.optimizer.state_dict(),
            'model_state_dict': model_state_dic,
            'best_mae': self.best_mae,
            'best_mse': self.best_mse,
            'best_mae_at': self.best_mae_at,
        }, save_path)
        self.save_list.append(save_path)  # control the number of saved models

    def val_epoch(self):
        epoch_start = time.time()
        self.model.eval()
        epoch_res = []

        for inputs, count, name in tqdm(self.dataloaders['val']):
            inputs = inputs.to(self.device)
            # inputs are images with different sizes
            b, c, h, w = inputs.shape
            h, w = int(h), int(w)
            assert b == 1, 'the batch size should equal to 1 in validation mode'

            max_size = 2000
            if h > max_size or w > max_size:
                h_stride = int(ceil(1.0 * h / max_size))
                w_stride = int(ceil(1.0 * w / max_size))
                h_step = h // h_stride
                w_step = w // w_stride
                input_list = []
                for i in range(h_stride):
                    for j in range(w_stride):
                        h_start = i * h_step
                        if i != h_stride - 1:
                            h_end = (i + 1) * h_step
                        else:
                            h_end = h
                        w_start = j * w_step
                        if j != w_stride - 1:
                            w_end = (j + 1) * w_step
                        else:
                            w_end = w
                        input_list.append(inputs[:, :, h_start:h_end, w_start:w_end])
                with torch.set_grad_enabled(False):
                    pre_count = 0.0
                    for input in input_list:
                        output1, output2 = self.model(input)
                        pre_count += (torch.sum(output1) + torch.sum(output2)) / 2
            else:
                with torch.set_grad_enabled(False):
                    output1, output2 = self.model(inputs)
                    pre_count = (torch.sum(output1) + torch.sum(output2)) / 2

            epoch_res.append(count[0].item() - pre_count.item() / self.args.log_param)

        epoch_res = np.array(epoch_res)
        mse = np.sqrt(np.mean(np.square(epoch_res)))
        mae = np.mean(np.abs(epoch_res))

        logging.info('Epoch {} Val, MSE: {:.2f} MAE: {:.2f}, Cost {:.1f} sec'
                     .format(self.epoch, mse, mae, time.time() - epoch_start))

        model_state_dic = self.model.state_dict()
        if mae < self.best_mae:
            self.best_mse = mse
            self.best_mae = mae
            self.best_mae_at = self.epoch
            logging.info("SAVE best mse {:.2f} mae {:.2f} model @epoch {}".format(self.best_mse, self.best_mae, self.epoch))
            if self.args.save_all:
                torch.save(model_state_dic, os.path.join(self.save_dir, 'best_model_{}.pth'.format(self.best_count)))
                self.best_count += 1
            else:
                torch.save(model_state_dic, os.path.join(self.save_dir, 'best_model.pth'))

        logging.info("best mae {:.2f} mse {:.2f} @epoch {}".format(self.best_mae, self.best_mse, self.best_mae_at))

        if self.epoch is not None:
            wandb.log({'Val/bestMAE': self.best_mae,
                       'Val/MAE': mae,
                       'Val/MSE': mse,
                      }, step=self.epoch)


