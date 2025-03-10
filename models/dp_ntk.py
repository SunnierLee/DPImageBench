import os
import logging

import numpy as np
import torch
from torch.optim.lr_scheduler import StepLR
import torchvision

import importlib
opacus = importlib.import_module('opacus')
from opacus.accountants.utils import get_noise_multiplier


from models.synthesizer import DPSynther
from models.DP_NTK.dp_ntk_mean_emb1 import calc_mean_emb1
from models.DP_NTK.ntk import *

class DP_NTK(DPSynther):
    def __init__(self, config, device):
        super().__init__()

        self.config = config
        self.device = device
        self.ntk_width = config.ntk_width
        self.input_dim = config.input_dim
        self.n_classes = config.n_classes

        self.z_dim = config.z_dim
        self.h_dim = config.h_dim
        self.n_channels = config.n_channels
        self.kernel_sizes = config.kernel_sizes

        if config.model_ntk == 'fc_1l':
            self.model_ntk = NTK(input_size=self.input_dim, hidden_size_1=self.ntk_width, output_size=self.n_classes)
        elif config.model_ntk == 'lenet5':
            self.model_ntk = LeNet5()
        else:
            raise NotImplementedError('{} is not yet implemented.'.format(config.model_ntk))

        self.model_ntk.to(device)
        self.model_ntk.eval()

        self.model_gen = ConvCondGen(self.z_dim, self.h_dim, self.n_classes, self.n_channels, self.kernel_sizes, self.input_dim).to(device)
        self.model_gen.train()

    def train(self, sensitive_dataloader, config):
        os.mkdir(config.log_dir)
        # define loss function
        self.noise_factor = get_noise_multiplier(target_epsilon=config.dp.epsilon, target_delta=config.dp.delta, sample_rate=1., epochs=1)

        logging.info("The noise factor is {}".format(self.noise_factor))

        self.noisy_mean_emb = calc_mean_emb1(self.model_ntk, sensitive_dataloader, self.n_classes, self.noise_factor, self.device)
        print(self.noisy_mean_emb.shape)

        torch.save(self.noisy_mean_emb, os.path.join(config.log_dir, 'noisy_mean_emb.pt'))


        optimizer = torch.optim.Adam(self.model_gen.parameters(), lr=config.lr)
        scheduler = StepLR(optimizer, step_size=1, gamma=config.lr_decay)

        """ initialize the variables """
        mean_v_samp = torch.Tensor([]).to(self.device)
        for p in self.model_ntk.parameters():
            mean_v_samp = torch.cat((mean_v_samp, p.flatten()))
        d = len(mean_v_samp)
        logging.info('Feature Length: {}'.format(d))

        """ training a Generator via minimizing MMD """
        for epoch in range(config.n_iter):  # loop over the dataset multiple times
            running_loss = 0.0
            optimizer.zero_grad()  # zero the parameter gradients

            """ synthetic data """
            gen_code, gen_labels = self.model_gen.get_code(config.batch_size, self.device)
            gen_code = gen_code.to(self.device)
            gen_samples = self.model_gen(gen_code)
            _, gen_labels_numerical = torch.max(gen_labels, dim=1)

            """ synthetic data mean_emb init """
            mean_emb2 = torch.zeros((d, self.n_classes), device=self.device)
            for idx in range(gen_samples.shape[0]):
                """ manually set the weight if needed """
                # model_ntk.fc1.weight = torch.nn.Parameter(output_weights[gen_labels_numerical[idx], :][None, :])
                mean_v_samp = torch.Tensor([]).to(self.device)  # sample mean vector init
                f_x = self.model_ntk(gen_samples[idx][None, :])

                """ get NTK features """
                f_idx_grad = torch.autograd.grad(f_x, self.model_ntk.parameters(),
                                                grad_outputs=f_x.data.new(f_x.shape).fill_(1), create_graph=True)
                for g in f_idx_grad:
                    mean_v_samp = torch.cat((mean_v_samp, g.flatten()))
                # mean_v_samp = mean_v_samp[:-1]

                """ normalize the sample mean vector """
                mean_emb2[:, gen_labels_numerical[idx]] += mean_v_samp / torch.linalg.vector_norm(mean_v_samp)

            """ average by batch size """
            mean_emb2 = mean_emb2 / config.batch_size

            """ calculate loss """
            loss = torch.norm(self.noisy_mean_emb - mean_emb2, p=2) ** 2
            loss.backward()
            optimizer.step()

            running_loss += loss.item()
            if (epoch + 1) % config.log_interval == 0:
                logging.info('iter {} and running loss are {}'.format(epoch, running_loss))
            if epoch % config.scheduler_interval == 0:
                scheduler.step()
        
        torch.save(self.model_gen.state_dict(), os.path.join(config.log_dir, 'gen.pt'))

# def gen_step(model_ntk, ar, device):
#     random.seed(ar.seed)
#     pt.manual_seed(ar.seed)

#     """ setup """
#     input_dim = 784
#     image_size = 28
#     n_data = 60_000
#     n_classes = 10

#     print('data: ', ar.data)
#     model = ConvCondGen(ar.d_code, ar.gen_spec, n_classes, '16,8', '5,5').to(device)

#     optimizer = optim.Adam(model.parameters(), lr=ar.lr)
#     scheduler = StepLR(optimizer, step_size=1, gamma=ar.lr_decay)
#     training_loss_per_epoch = np.zeros(ar.n_iter)

#     """ initialize the variables """
#     mean_v_samp = pt.Tensor([]).to(device)
#     for p in model_ntk.parameters():
#         mean_v_samp = pt.cat((mean_v_samp, p.flatten()))
#     d = len(mean_v_samp)
#     print('Feature Length:', d)

#     if ar.tgt_eps is None:
#         noise_mean_emb1 = pt.load(ar.save_dir + f'mean_emb1_{d}_{ar.seed}.pth')
#     else:
#         noise_mean_emb1 = pt.load(ar.save_dir + f'mean_emb1_{d}_{ar.seed}_{ar.tgt_eps}.pth')
#     model_ntk.load_state_dict(pt.load(ar.save_dir + f'model_{d}_{ar.seed}.pth', map_location=device))

#     """ random noise func for generators """
#     code_fun = model.get_code

#     """ generate 100 samples for output log """
#     fixed_labels = pt.repeat_interleave(pt.arange(10, device=device), 10)
#     print(fixed_labels.shape)
#     fixed_noise = code_fun(batch_size=100, labels=fixed_labels[:, None])

#     """ training a Generator via minimizing MMD """
#     for epoch in range(ar.n_iter):  # loop over the dataset multiple times
#         running_loss = 0.0
#         optimizer.zero_grad()  # zero the parameter gradients

#         """ synthetic data """
#         gen_code, gen_labels = code_fun(ar.batch_size)
#         gen_code = gen_code.to(device)
#         gen_samples = model(gen_code)
#         _, gen_labels_numerical = pt.max(gen_labels, dim=1)

#         """ synthetic data mean_emb init """
#         mean_emb2 = pt.zeros((d, n_classes), device=device)
#         for idx in range(gen_samples.shape[0]):
#             """ manually set the weight if needed """
#             # model_ntk.fc1.weight = torch.nn.Parameter(output_weights[gen_labels_numerical[idx], :][None, :])
#             mean_v_samp = pt.Tensor([]).to(device)  # sample mean vector init
#             f_x = model_ntk(gen_samples[idx][None, :])

#             """ get NTK features """
#             f_idx_grad = pt.autograd.grad(f_x, model_ntk.parameters(),
#                                              grad_outputs=f_x.data.new(f_x.shape).fill_(1), create_graph=True)
#             for g in f_idx_grad:
#                 mean_v_samp = pt.cat((mean_v_samp, g.flatten()))
#             # mean_v_samp = mean_v_samp[:-1]

#             """ normalize the sample mean vector """
#             mean_emb2[:, gen_labels_numerical[idx]] += mean_v_samp / pt.linalg.vector_norm(mean_v_samp)

#         """ average by batch size """
#         mean_emb2 = pt.div(mean_emb2, ar.batch_size)

#         """ calculate loss """
#         loss = pt.norm(noise_mean_emb1 - mean_emb2, p=2) ** 2
#         loss.backward()
#         optimizer.step()

#         running_loss += loss.item()
#         if (epoch + 1) % ar.log_interval == 0:
#             log_gen_data(model, device, epoch, n_classes, ar.log_dir)
#             print('epoch # and running loss are ', [epoch, running_loss])
#             training_loss_per_epoch[epoch] = running_loss
#         if epoch % ar.scheduler_interval == 0:
#             scheduler.step()
    def generate(self, config):
        os.mkdir(config.log_dir)

        """evaluate the model"""
        syn_data, syn_labels = synthesize_mnist_with_uniform_labels(self.model_gen, self.device, gen_batch_size=config.batch_size, n_data=config.data_num, n_labels=self.n_classes)
        syn_data = syn_data.reshape(syn_data.shape[0], config.num_channels, config.resolution, config.resolution)
        syn_labels = syn_labels.reshape(-1)
        np.savez(os.path.join(config.log_dir, "gen.npz"), x=syn_data, y=syn_labels)

        show_images = []
        for cls in range(self.n_classes):
            show_images.append(syn_data[syn_labels==cls][:8])
        show_images = np.concatenate(show_images)
        torchvision.utils.save_image(torch.from_numpy(show_images), os.path.join(config.log_dir, 'sample.png'), padding=1, nrow=8)
        return syn_data, syn_labels


class ConvCondGen(nn.Module):
    def __init__(self, d_code, d_hid, n_labels, nc_str, ks_str, n_feats, use_sigmoid=True, batch_norm=True):
        super(ConvCondGen, self).__init__()
        self.nc = [int(k) for k in nc_str.split(',')]
        self.ks = [int(k) for k in ks_str.split(',')]  # kernel sizes
        d_hid = [int(k) for k in d_hid.split(',')]
        assert len(self.nc) == 3 and len(self.ks) == 2
        self.hw = int(np.sqrt(n_feats // self.nc[-1])) // 4
        self.reshape_size = self.nc[0]*self.hw**2
        self.fc1 = nn.Linear(d_code + n_labels, d_hid[0])
        self.fc2 = nn.Linear(d_hid[0], self.reshape_size)
        self.bn1 = nn.BatchNorm1d(d_hid[0]) if batch_norm else None
        self.bn2 = nn.BatchNorm1d(self.reshape_size) if batch_norm else None
        self.conv1 = nn.Conv2d(self.nc[0], self.nc[1], kernel_size=self.ks[0], stride=1, padding=(self.ks[0]-1)//2)
        self.conv2 = nn.Conv2d(self.nc[1], self.nc[2], kernel_size=self.ks[1], stride=1, padding=(self.ks[1]-1)//2)
        self.upsamp = nn.UpsamplingBilinear2d(scale_factor=2)
        self.relu = nn.ReLU()
        self.sigmoid = nn.Sigmoid()
        self.use_sigmoid = use_sigmoid
        self.d_code = d_code
        self.n_labels = n_labels

    def forward(self, x):
        x = self.fc1(x)
        x = self.bn1(x) if self.bn1 is not None else x
        x = self.fc2(self.relu(x))
        x = self.bn2(x) if self.bn2 is not None else x
        # print(x.shape)
        x = x.reshape(x.shape[0], self.nc[0], self.hw, self.hw)
        x = self.upsamp(x)
        x = self.relu(self.conv1(x))
        x = self.upsamp(x)
        x = self.conv2(x)
        x = x.reshape(x.shape[0], -1)
        if self.use_sigmoid:
            x = self.sigmoid(x)
        return x
    def get_code(self, batch_size, device, return_labels=True, labels=None):
        if labels is None:  # sample labels
            labels = torch.randint(self.n_labels, (batch_size, 1), device=device)
        code = torch.randn(batch_size, self.d_code, device=device)
        gen_one_hots = torch.zeros(batch_size, self.n_labels, device=device)
        gen_one_hots.scatter_(1, labels, 1)
        code = torch.cat([code, gen_one_hots.to(torch.float32)], dim=1)
        # print(code.shape)
        if return_labels:
            return code, gen_one_hots
        else:
            return code


def synthesize_mnist_with_uniform_labels(gen, device, gen_batch_size=1000, n_data=60000, n_labels=10):
    gen.eval()
    assert n_data % gen_batch_size == 0
    assert gen_batch_size % n_labels == 0
    n_iterations = n_data // gen_batch_size

    data_list = []
    ordered_labels = torch.repeat_interleave(torch.arange(n_labels), gen_batch_size // n_labels)[:, None].to(device)
    labels_list = [ordered_labels] * n_iterations

    with torch.no_grad():
        for idx in range(n_iterations):
            gen_code, gen_labels = gen.get_code(gen_batch_size, device, labels=ordered_labels)
            gen_samples = gen(gen_code)
            data_list.append(gen_samples)
    return torch.cat(data_list, dim=0).cpu().numpy(), torch.cat(labels_list, dim=0).cpu().numpy()


def train_single_release(gen, device, optimizer, epoch, rff_mmd_loss, log_interval, batch_size, n_data):
    n_iter = n_data // batch_size
    for batch_idx in range(n_iter):
        gen_code, gen_labels = gen.get_code(batch_size, device)
        loss = rff_mmd_loss(gen(gen_code), gen_labels)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        if batch_idx % log_interval == 0:
            logging.info('Train Epoch: {} [{}/{}]\tLoss: {:.6f}'.format(epoch, batch_idx * batch_size, n_data, loss.item()))