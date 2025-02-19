import argparse
import numpy as np
import cv2
import os
import time

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
from torch.utils.data import DataLoader
from tqdm import tqdm

from model import FFDNet
import utils

def read_image(image_path, is_gray):
    """
    :return: Normalized Image (C * W * H)
    """
    if is_gray:
        image = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        image = np.expand_dims(image.T, 0) # 1 * W * H
    else:
        image = cv2.imread(image_path)
        image = (cv2.cvtColor(image, cv2.COLOR_BGR2RGB)).transpose(2, 1, 0) # 3 * W * H
    
    return utils.normalize(image)

def load_images(is_train, is_gray, base_path):
    """
    :param base_path: ./train_data/
    :return: List[Patches] (C * W * H)
    """
    if is_gray:
        train_dir = 'gray/train/'
        val_dir = 'gray/val/'
    else:
        train_dir = 'rgb/train/'
        val_dir = 'rgb/val/'
    
    image_dir = base_path.replace('\'', '').replace('"', '') + (train_dir if is_train else val_dir)
    print('> Loading images in ' + image_dir)
    images = []
    for fn in next(os.walk(image_dir))[2]:
        image = read_image(image_dir + fn, is_gray)
        images.append(image)
    return images

def images_to_patches(images, patch_size):
    """
    :param images: List[Image (C * W * H)]
    :param patch_size: int
    :return: (n * C * W * H)
    """
    patches_list = []
    for image in images:
        patches = utils.image_to_patches(image, patch_size=patch_size)
        if len(patches) != 0:
            patches_list.append(patches)
    del images
    return np.vstack(patches_list)

def train(args):
    print('> Loading dataset...')
    # Images
    train_dataset = load_images(is_train=True, is_gray=args.is_gray, base_path=args.train_path)
    val_dataset = load_images(is_train=False, is_gray=args.is_gray, base_path=args.train_path)
    print(f'\tTrain image datasets: {len(train_dataset)}')
    print(f'\tVal image datasets: {len(val_dataset)}')

    # Patches
    train_dataset = images_to_patches(train_dataset, patch_size=args.patch_size)
    val_dataset = images_to_patches(val_dataset, patch_size=args.patch_size)
    print(f'\tTrain patch datasets: {train_dataset.shape}')
    print(f'\tVal patch datasets: {val_dataset.shape}')

    # DataLoader
    train_dataloader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=6)
    val_dataloader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False, num_workers=6)
    print(f'\tTrain batch number: {len(train_dataloader)}')
    print(f'\tVal batch number: {len(val_dataloader)}')

    # Noise list
    train_noises = args.train_noise_interval # [0, 75, 15]
    val_noises = args.val_noise_interval # [0, 60, 30]
    train_noises = list(range(train_noises[0], train_noises[1], train_noises[2]))
    val_noises = list(range(val_noises[0], val_noises[1], val_noises[2]))
    print(f'\tTrain noise internal: {train_noises}')
    print(f'\tVal noise internal: {val_noises}')
    print('\n')

    # Model & Optim
    model = FFDNet(is_gray=args.is_gray)
    model.apply(utils.weights_init_kaiming)
    if args.cuda:
        device = torch.device('cuda:8' if torch.cuda.is_available() else 'cpu')
    else:
        device = torch.device('cpu')
    model = model.to(device)
    loss_fn = nn.MSELoss(reduction='sum')
    optimizer = optim.Adam(model.parameters(), lr=args.learning_rate)

    print('> Start training...')
    for epoch_idx in range(args.epoches):
        # Train
        loss_idx = 0
        train_losses = 0
        model.train()

        start_time = time.time()
        for batch_idx, batch_data in enumerate(train_dataloader):
            # According to internal, add noise
            for int_noise_sigma in train_noises:
                noise_sigma = int_noise_sigma / 255
                new_images = utils.add_batch_noise(batch_data, noise_sigma)
                noise_sigma = torch.FloatTensor(np.array([noise_sigma for idx in range(new_images.shape[0])]))
                new_images = Variable(new_images)
                noise_sigma = Variable(noise_sigma)
                if args.cuda:
                    new_images = new_images.to(device)
                    noise_sigma = noise_sigma.to(device)

                # Predict
                images_pred = model(new_images, noise_sigma, device)
                train_loss = loss_fn(images_pred, batch_data.to(images_pred.device))
                train_losses += train_loss
                loss_idx += 1

                optimizer.zero_grad()
                train_loss.backward()
                optimizer.step()

                # Log Progress
                stop_time = time.time()
                all_num = len(train_dataloader) * len(train_noises)
                done_num = batch_idx * len(train_noises) + train_noises.index(int_noise_sigma) + 1
                rest_time = int((stop_time - start_time) / done_num * (all_num - done_num))
                percent = int(done_num / all_num * 100)
                print(f'\rEpoch: {epoch_idx + 1} / {args.epoches}, ' +
                      f'Batch: {batch_idx + 1} / {len(train_dataloader)}, ' +
                      f'Noise_Sigma: {int_noise_sigma} / {train_noises[-1]}, ' +
                      f'Train_Loss: {train_loss}, ' +
                      f'=> {rest_time}s, {percent}%', end='')

                # if args.cuda:
                #     new_images = new_images.cpu()
                #     noise_sigma = noise_sigma.cpu()
                #     torch.cuda.empty_cache()

        train_losses /= loss_idx
        print(f', Avg_Train_Loss: {train_losses}, All: {int(stop_time - start_time)}s')
        
        # Evaluate
        loss_idx = 0
        val_losses = 0

        if (epoch_idx + 1) % 5 == 0:
            model_path = args.model_path + ('net_gray.pth' if args.is_gray else 'net_rgb.pth')
            torch.save(model.state_dict(), model_path)
            print(f'Saved State Dict in {model_path}')
            print('\n')
        else:
            continue
        
        start_time = time.time()
        for batch_idx, batch_data in enumerate(val_dataloader):
            # According to internal, add noise
            for int_noise_sigma in val_noises:
                noise_sigma = int_noise_sigma / 255
                new_images = utils.add_batch_noise(batch_data, noise_sigma)
                noise_sigma = torch.FloatTensor(np.array([noise_sigma for idx in range(new_images.shape[0])]))
                new_images = Variable(new_images)
                noise_sigma = Variable(noise_sigma)
                if args.cuda:
                    new_images = new_images.cuda()
                    noise_sigma = noise_sigma.cuda()
                
                # Predict
                images_pred = model(new_images, noise_sigma)
                val_loss = loss_fn(images_pred, batch_data.to(images_pred.device))
                val_losses += val_loss
                loss_idx += 1
                
                # Log Progress
                stop_time = time.time()
                all_num = len(val_dataloader) * len(val_noises)
                done_num = batch_idx * len(val_noises) + val_noises.index(int_noise_sigma) + 1
                rest_time = int((stop_time - start_time) / done_num * (all_num - done_num))
                percent = int(done_num / all_num * 100)
                print(f'\rEpoch: {epoch_idx + 1} / {args.epoches}, ' +
                      f'Batch: {batch_idx + 1} / {len(val_dataloader)}, ' +
                      f'Noise_Sigma: {int_noise_sigma} / {val_noises[-1]}, ' +
                      f'Val_Loss: {val_loss}, ' + 
                      f'=> {rest_time}s, {percent}%', end='')
                if args.cuda:
                    new_images = new_images.cpu()
                    noise_sigma = noise_sigma.cpu()
                    torch.cuda.empty_cache()
                
        val_losses /= loss_idx
        print(f', Avg_Val_Loss: {val_losses}, All: {int(stop_time - start_time)}s')

    # Final Save Model Dict
    model.eval()
    model_path = args.model_path + ('net_gray.pth' if args.is_gray else 'net_rgb.pth')
    torch.save(model.state_dict(), model_path)
    print(f'Saved State Dict in {model_path}')
    print('\n')

def test(args):
    # Image
    # Dict
    is_gray = True
    model_path = args.model_path + ('net_gray.pth' if is_gray else 'net_rgb.pth')
    print(f"> Loading model param in {model_path}...")
    model = FFDNet(is_gray=is_gray)
    state_dict = torch.load(model_path)
    model.load_state_dict(state_dict)
    model = model.cuda()
    model.eval()
    print('\n')
    images = os.listdir(args.test_path)
    for image_name in tqdm(images):
        image_path = os.path.join(args.test_path, image_name)
        image = cv2.imread(image_path)
        if image is None:
            raise Exception(f'File {args.test_path} not found or error')
        image = read_image(image_path, is_gray)
        # print("{} image shape: {}".format("Gray" if is_gray else "RGB", image.shape))

        # Expand odd shape to even
        expend_W = False
        expend_H = False
        if image.shape[1] % 2 != 0:
            expend_W = True
            image = np.concatenate((image, image[:, -1, :][:, np.newaxis, :]), axis=1)
        if image.shape[2] % 2 != 0:
            expend_H = True
            image = np.concatenate((image, image[:, :, -1][:, :, np.newaxis]), axis=2)
        
        # Noise
        image = torch.FloatTensor([image]) # 1 * C(1 / 3) * W * H
        if args.add_noise:
            image = utils.add_batch_noise(image, args.noise_sigma)
        noise_sigma = torch.FloatTensor([args.noise_sigma])

        # Model & GPU
        if args.cuda:
            image = image.cuda()
            noise_sigma = noise_sigma.cuda()
        
        # Test
        with torch.no_grad():
            start_time = time.time()
            image_pred = model(image, noise_sigma)
            stop_time = time.time()
            # print("Test time: {0:.4f}s".format(stop_time - start_time))

        # PSNR
        # psnr = utils.batch_psnr(img=image_pred, imclean=image, data_range=1)
        # print("PSNR denoised {0:.2f}dB".format(psnr))

        # UnExpand odd
        if expend_W:
            image_pred = image_pred[:, :, :-1, :]
        if expend_H:
            image_pred = image_pred[:, :, :, :-1]

        # Save
        cv2.imwrite(f"./outputs/{image_name}.png", utils.variable_to_cv2_image(image_pred).T)
        if args.add_noise:
            cv2.imwrite("noisy.png", utils.variable_to_cv2_image(image))

def main():
    parser = argparse.ArgumentParser()

    # Train
    parser.add_argument("--train_path", type=str, default='./train_data/',                  help='Train dataset dir.')
    parser.add_argument("--is_gray", action='store_true',                                   help='Train gray/rgb model.')
    parser.add_argument("--patch_size", type=int, default=32,                               help='Uniform size of training images patches.')
    parser.add_argument("--train_noise_interval", nargs=3, type=int, default=[0, 75, 15],   help='Train dataset noise sigma set interval.')
    parser.add_argument("--val_noise_interval", nargs=3, type=int, default=[0, 60, 30],     help='Validation dataset noise sigma set interval.')
    parser.add_argument("--batch_size", type=int, default=256,                              help='Batch size for training.')
    parser.add_argument("--epoches", type=int, default=80,                                  help='Total number of training epoches.')
    parser.add_argument("--val_epoch", type=int, default=5,                                 help='Total number of validation epoches.')
    parser.add_argument("--learning_rate", type=float, default=1e-3,                        help='The initial learning rate for Adam.')
    parser.add_argument("--save_checkpoints", type=int, default=5,                          help='Save checkpoint every epoch.')

    # Test
    parser.add_argument("--test_path", type=str, default='./test_data/color.png',           help='Test image path.')
    parser.add_argument("--noise_sigma", type=float, default=25,                            help='Input uniform noise sigma for test.')
    parser.add_argument('--add_noise', action='store_true',                                 help='Add noise_sigma to input or not.')

    # Global
    parser.add_argument("--model_path", type=str, default='./models/',                      help='Model loading and saving path.')
    parser.add_argument("--use_gpu", action='store_true',                                   help='Train and test using GPU.')
    parser.add_argument("--is_train", action='store_true',                                  help='Do train.')
    parser.add_argument("--is_test", action='store_true',                                   help='Do test.')

    args = parser.parse_args()
    assert (args.is_train or args.is_test), 'is_train 和 is_test 至少有一个为 True'

    args.cuda = args.use_gpu and torch.cuda.is_available()
    print("> Parameters: ")
    for k, v in zip(args.__dict__.keys(), args.__dict__.values()):
        print(f'\t{k}: {v}')
    print('\n')

    # Normalize noise level
    args.noise_sigma /= 255
    args.train_noise_interval[1] += 1
    args.val_noise_interval[1] += 1

    if args.is_train:
        train(args)

    if args.is_test:
        test(args)

if __name__ == "__main__":
    main()
