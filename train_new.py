#!/usr/bin/env python

import os
import sys
import math
import argparse
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler

from torch.autograd import Variable
from torch.utils.data import DataLoader

import torchvision
from torchvision.utils import save_image
import torchvision.transforms as transforms

from utils import *
from data import get_training_set, get_test_set
from models import Generator, Discriminator, FeatureExtractor


parser = argparse.ArgumentParser()
parser.add_argument('--inputs', type=str, default='set', help='oil')
parser.add_argument('--threads', type=int, default=12, help='number of data loading workers')
parser.add_argument('--batchSize', type=int, default=16, help='input batch size')
parser.add_argument('--test_batchSize', type=int, default=10, help='input batch size')
parser.add_argument('--crop_size', type=int, default=80, help='the low resolution image size')
parser.add_argument('--upFactor', type=int, default=4, help='low to high resolution scaling factor')
parser.add_argument('--nPreEpochs', type=int, default=300, help='pretrain number of epochs to train for')
parser.add_argument('--nEpochs', type=int, default=500, help='number of epochs to train for')
parser.add_argument('--generatorLR', type=float, default=0.0001, help='learning rate for generator')
parser.add_argument('--discriminatorLR', type=float, default=0.0001, help='learning rate for discriminator')
parser.add_argument('--train_jpeg', type=int, default=0, help='jpeg image quality range(1-15). Default=10')
parser.add_argument('--train_noise', type=float, default=0.0, help='Add gaussian noise in train. Default=1.0')
parser.add_argument('--train_blur', type=float, default=0.0, help='Add gaussian blur in train. Default=1.0')
parser.add_argument('--cuda', action='store_true', help='enables cuda')
parser.add_argument('--nGPU', type=int, default=1, help='number of GPUs to use')
parser.add_argument('--seed', type=int, default=123, help='random seed')
parser.add_argument('--generatorWeights', type=str, default='', help="path to generator weights (to continue training)")
parser.add_argument('--discriminatorWeights', type=str, default='', help="path to discriminator weights (to continue training)")
parser.add_argument('--out', type=str, default='checkpoints', help='folder to output model checkpoints')
parser.add_argument('--train_output', type=str, default='train_output', help='folder to output train results')

opt = parser.parse_args()
print('===> Parameters')
print(opt)

try:
    os.makedirs(opt.out)
except OSError:
    pass
try:
    os.makedirs(opt.train_output)
except OSError:
    pass

if torch.cuda.is_available() and not opt.cuda:
    print("WARNING: You have a CUDA device, so you should probably run with --cuda")

# Equivalent to un-normalizing ImageNet (for correct visualization)
unnormalize = transforms.Normalize(mean = [-2.118, -2.036, -1.804], std = [4.367, 4.464, 4.444])

torch.manual_seed(opt.seed)
if opt.cuda:
    torch.cuda.manual_seed(opt.seed)

print('===> Loading datasets')
train_set = get_training_set(opt.inputs, opt.crop_size, opt.upFactor, opt.train_jpeg, opt.train_noise, opt.train_blur)
test_set = get_test_set(opt.inputs, opt.crop_size, opt.upFactor, opt.train_jpeg)
train_dataloader = DataLoader(dataset=train_set, num_workers=opt.threads, batch_size=opt.batchSize, shuffle=True, drop_last=True)
test_dataloader = DataLoader(dataset=test_set, num_workers=opt.threads, batch_size=opt.test_batchSize, shuffle=False, drop_last=True)

generator = Generator(opt.upFactor)
if opt.generatorWeights != '':
    generator.load_state_dict(torch.load(opt.generatorWeights))

discriminator = Discriminator()
if opt.discriminatorWeights != '':
    discriminator.load_state_dict(torch.load(opt.discriminatorWeights))

# For the content loss
feature_extractor = FeatureExtractor(torchvision.models.vgg19(pretrained=True))
content_criterion = nn.MSELoss()

adversarial_criterion = nn.BCELoss()
ones_const = Variable(torch.ones(opt.batchSize, 1))

psnr_mse = nn.MSELoss()

# if gpu is to be used
if opt.cuda:
    generator = generator.cuda()
    discriminator = discriminator.cuda()
    feature_extractor = feature_extractor.cuda()
    content_criterion = content_criterion.cuda()
    adversarial_criterion = adversarial_criterion.cuda()
    ones_const = ones_const.cuda()
    psnr_mse = psnr_mse.cuda()

# Pre-train generator using raw MSE loss
def pre_train(epoch):
    mean_generator_content_loss = 0.0

    for i, batch in enumerate(train_dataloader, 1):
        # Generate data
        low_res, high_res_real = batch

        # Generate real and fake inputs
        high_res_real = Variable(high_res_real)
        low_res = Variable(low_res)
        if opt.cuda:
            high_res_real = high_res_real.cuda()
            low_res = low_res.cuda()

        high_res_fake = generator(low_res)

        ######### Train generator #########
        generator.zero_grad()

        generator_content_loss = content_criterion(high_res_fake, high_res_real)
        mean_generator_content_loss += float(generator_content_loss.data)

        generator_content_loss.backward()
        optim_generator.step()

        ######### Status and display #########
        sys.stdout.write('\r[%d/%d][%d/%d] Generator_MSE_Loss: %.4f' % (epoch, opt.nPreEpochs, i, len(train_dataloader), float(generator_content_loss.data)))

    sys.stdout.write('\r[%d/%d][%d/%d] Generator_MSE_Loss: %.4f\n' % (epoch, opt.nPreEpochs, i, len(train_dataloader), mean_generator_content_loss/len(train_dataloader)))


# SRGAN training
def train(epoch):
    mean_generator_content_loss = 0.0
    mean_generator_adversarial_loss = 0.0
    mean_generator_total_loss = 0.0
    mean_discriminator_loss = 0.0

    for i, batch in enumerate(train_dataloader, 1):
        # Generate data
        low_res, high_res_real = batch

        # Generate real and fake inputs
        high_res_real = Variable(high_res_real)
        low_res = Variable(low_res)
        target_real = Variable(torch.rand(opt.batchSize, 1)*0.5 + 0.7)
        target_fake = Variable(torch.rand(opt.batchSize, 1)*0.3)

        if opt.cuda:
            high_res_real = high_res_real.cuda()
            low_res = low_res.cuda()
            target_real = target_real.cuda()
            target_fake = target_fake.cuda()
        
        high_res_fake = generator(low_res)

        ######### Train discriminator #########
        discriminator.zero_grad()

        discriminator_loss = adversarial_criterion(discriminator(high_res_real), target_real) + \
                             adversarial_criterion(discriminator(Variable(high_res_fake.data)), target_fake)
        mean_discriminator_loss += float(discriminator_loss.data)
        
        discriminator_loss.backward()
        optim_discriminator.step()

        ######### Train generator #########
        generator.zero_grad()

        real_features = Variable(feature_extractor(high_res_real).data)
        fake_features = feature_extractor(high_res_fake)

        generator_content_loss = content_criterion(high_res_fake, high_res_real) + 0.000*content_criterion(fake_features, real_features)
        mean_generator_content_loss += float(generator_content_loss.data)
        generator_adversarial_loss = adversarial_criterion(discriminator(high_res_fake), ones_const)
        mean_generator_adversarial_loss += float(generator_adversarial_loss.data)

        generator_total_loss = generator_content_loss + 1e-3*generator_adversarial_loss
        mean_generator_total_loss += float(generator_total_loss.data)
        
        generator_total_loss.backward()
        optim_generator.step()   
        
        ######### Status and display #########
        sys.stdout.write('\r[%d/%d][%d/%d] Discriminator_Loss: %.4f Generator_Loss (Content/Advers/Total): %.4f/%.4f/%.4f' % (epoch, opt.nEpochs, i, len(train_dataloader),
        float(discriminator_loss.data), float(generator_content_loss.data), float(generator_adversarial_loss.data),float(generator_total_loss.data)))

    sys.stdout.write('\r[%d/%d][%d/%d] Discriminator_Loss: %.4f Generator_Loss (Content/Advers/Total): %.4f/%.4f/%.4f\n' % (epoch, opt.nEpochs, i,
    len(train_dataloader), mean_discriminator_loss/len(train_dataloader), mean_generator_content_loss/len(train_dataloader),
    mean_generator_adversarial_loss/len(train_dataloader), mean_generator_total_loss/len(train_dataloader)))

def test():
    avg_psnr = 0
    generator.eval()
    flag = True
    for batch in test_dataloader:
        input, target = Variable(batch[0]), Variable(batch[1])
        if opt.cuda:
            input = input.cuda()
            target = target.cuda()

        prediction = generator(input)
        
        input_data = input.data
        prediction_data = unnormalize(prediction.data)
        target_data = unnormalize(target.data)
        if flag:
            for j in range(opt.test_batchSize):
                save_image(target_data[j], '{}/{:04d}x{}_GT.png'.format(opt.train_output, j, opt.upFactor))
                save_image(input_data[j], '{}/{:04d}x{}_LR.png'.format(opt.train_output, j, opt.upFactor))
                save_image(prediction_data[j], '{}/{:04d}x{}_SR.png'.format(opt.train_output, j, opt.upFactor))
            flag = False
        psnr = calcPSNR(prediction_data, target_data)
        avg_psnr += psnr
    print('Avg. PSNR: {:.4f} dB'.format(avg_psnr / len(test_dataloader)))

def checkpoint(epoch, pre_train=False):
    # Do checkpointing
    if epoch > 0:
        if pre_train:
            torch.save(generator.state_dict(), '%s/generator_pretrain_dict.pth' % opt.out)
        else:
            torch.save(generator.state_dict(), '%s/generator_final_dict.pth' % opt.out)
            torch.save(discriminator.state_dict(), '%s/discriminator_final_dict.pth' % opt.out)
        print('model dict saved to %s' % opt.out)
    else:
        torch.save(generator, '%s/generator.pth' % opt.out)
        torch.save(discriminator, '%s/discriminator.pth' % opt.out)
        print('whole mode saved to %s' % opt.out)


if __name__ == '__main__':
    print('===> Generator pre-training')
    optim_generator = optim.Adam(generator.parameters(), lr=opt.generatorLR)
    optim_discriminator = optim.Adam(discriminator.parameters(), lr=opt.discriminatorLR)
    for epoch in range(1, opt.nPreEpochs+1):
        pre_train(epoch)
        test()
        checkpoint(epoch, pre_train=True)
    print('===> SRGAN training')
    optim_generator = optim.Adam(generator.parameters(), lr=opt.generatorLR*0.1)
    optim_discriminator = optim.Adam(discriminator.parameters(), lr=opt.discriminatorLR*0.1)
    for epoch in range(1, opt.nEpochs + 1):
        train(epoch)
        test()
        checkpoint(epoch)
    checkpoint(-1)
