import argparse
import random
import time
import glob
from tqdm import tqdm   
import os

import numpy as np
import torch
import torch.nn as nn
import torchvision.datasets as datasets
import torchvision.transforms as transforms
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
from torch.utils.data import DataLoader

from arch.mnist import ClassifierMNIST, QuerierMNIST
import ops
import utils
import wandb



def parseargs():
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--data', type=str, default='mnist')
    parser.add_argument('--batch_size', type=int, default=128)
    parser.add_argument('--patch_size', type=int, default=4)
    parser.add_argument('--max_queries', type=int, default=676)
    parser.add_argument('--max_queries_test', type=int, default=20)
    parser.add_argument('--lr', type=float, default=0.05)
    parser.add_argument('--tau_start', type=float, default=1.0)
    parser.add_argument('--tau_end', type=float, default=0.2)
    parser.add_argument('--sampling', type=str, default='random')
    parser.add_argument('--optimizer', type=str, default='sgd', help='optimizer')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--mode', type=str, default='online')
    parser.add_argument('--tail', type=str, default='', help='tail message')
    parser.add_argument('--save_dir', type=str, default='./saved/', help='save directory')
    parser.add_argument('--data_dir', type=str, default='./data/', help='save directory')
    args = parser.parse_args()
    return args


def main(args):
    ## Setup
    # wandb
    run = wandb.init(project="Variational-IP", name="mnist", mode=args.mode)
    model_dir = os.path.join(args.save_dir, f'{run.id}')
    os.makedirs(model_dir, exist_ok=True)
    os.makedirs(os.path.join(model_dir, 'ckpt'), exist_ok=True)
    utils.save_params(model_dir, vars(args))
    wandb.config.update(args)

    # cuda
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    print('DEVICE:', device)

    # random
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)

    ## Constants
    QUERY_ALL = 676 # 26*26
    PATCH_SIZE = 3
    THRESHOLD = 0.85

    ## Data
    transform = transforms.Compose([transforms.ToTensor(),  
                                    transforms.Lambda(lambda x: torch.where(x < 0.5, -1., 1.))])
    trainset = datasets.MNIST(args.data_dir, train=True, transform=transform, download=True)
    testset = datasets.MNIST(args.data_dir, train=False, transform=transform, download=True)
    trainloader = DataLoader(trainset, batch_size=args.batch_size, num_workers=4)
    testloader = DataLoader(testset, batch_size=args.batch_size, num_workers=4)

    ## Model
    classifier = ClassifierMNIST()
    classifier = nn.DataParallel(classifier).to(device)
    querier = QuerierMNIST(num_classes=QUERY_ALL, tau=args.tau_start)
    querier = nn.DataParallel(querier).to(device)

    ## Optimization
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(list(querier.parameters()) + list(classifier.parameters()), 
                           amsgrad=True, lr=args.lr)
    scheduler = lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
    tau_vals = np.linspace(args.tau_start, args.tau_end, args.epochs)

    ## Train
    for epoch in range(args.epochs):
        tau = tau_vals[epoch]
        for images, labels in tqdm(trainloader):
            images = images.to(device)
            labels = labels.to(device)
            querier.module.update_tau(tau)

            # initial random sampling
            if args.sampling == 'adaptive':
                num_queries = torch.randint(low=0, high=QUERY_ALL, size=(images.size(0),))
                mask, masked_image = ops.adaptive_sampling(images, num_queries, querier, PATCH_SIZE, QUERY_ALL)
            elif args.sampling == 'random':
                mask = ops.random_sampling(args.max_queries, QUERY_ALL, images.size(0)).to(device)
                masked_image = ops.get_patch_mask(mask, images, patch_size=PATCH_SIZE)

            # Query and update
            query_vec = querier(masked_image, mask)
            masked_image = ops.update_masked_image(masked_image, images, query_vec, patch_size=PATCH_SIZE)

            # prediction
            train_logits = classifier(masked_image)

            # backprop
            loss = criterion(train_logits, labels)
            loss.backward()
            optimizer.step()

            # logging
            wandb.log({
                'epoch': epoch,
                'loss': loss.item(),
                'lr': utils.get_lr(optimizer),
                'gradnorm_cls': utils.get_grad_norm(classifier),
                'gradnorm_qry': utils.get_grad_norm(querier)
                })
        scheduler.step()

        # saving
        if epoch % 10 == 0 or epoch == args.epochs - 1:
            torch.save({
                'classifier': classifier.state_dict(),
                'querier': querier.state_dict(),
                'optimizer': optimizer.state_dict(),
                'scheduler': scheduler.state_dict()
                },
                os.path.join(model_dir, 'ckpt', f'epoch{epoch}.ckpt'))

        # evaluation
        if epoch % 10 == 0 or epoch == args.epochs - 1:
            for test_images, test_labels in tqdm(testloader):
                test_images = test_images.to(device)
                test_labels = test_labels.to(device)
                N, H, C, W = test_images.shape

                # Query
                test_inputs = torch.zeros_like(test_images).to(device)
                mask = torch.zeros(N, QUERY_ALL).to(device)
                logits, queries = [], []
                for i in range(args.max_queries_test):
                    query_vec = querier(test_inputs, mask)
                    label_logits = classifier(test_inputs)
                    mask[np.arange(N), query_vec.argmax(dim=1)] = 1.0
                    test_inputs = ops.update_masked_image(test_inputs, test_images, query_vec, patch_size=PATCH_SIZE)
                    logits.append(label_logits)
                    queries.append(query_vec)
                acc_max = (label_logits.argmax(dim=1).float() == test_labels.squeeze()).float().mean().item() \
				* (N / len(testset))
                logits = torch.stack(logits).permute(1, 0, 2)
                queries_needed = ops.compute_queries_needed(logits, threshold=THRESHOLD)
                test_pred_ip = logits[torch.arange(len(queries_needed)), queries_needed - 1].argmax(1)
                acc_ip = (test_pred_ip == test_labels.squeeze()).float().mean().item() * (N / len(testset))
                qry_need_avg = queries_needed.float().mean().item() * (N / len(testset))
                qry_need_std = queries_needed.float().std().item() * (N / len(testset))

            wandb.log({
                'test_epoch': epoch,
                'test_acc_max': acc_max,
                'test_acc_ip': acc_ip,
                'qry_need_avg': qry_need_avg,
                'qry_need_std': qry_need_std
                })


if __name__ == '__main__':
    args = parseargs()    
    main(args)


