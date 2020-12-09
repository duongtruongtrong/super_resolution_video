#!/usr/bin/env python
# coding: utf-8

# # 1. Set up Directories

# In[1]:

from data_loader import DataLoader

import os
import pathlib
import cv2
import matplotlib.pyplot as plt
import numpy as np

import tensorflow as tf
from tensorflow import keras

# When running the model with conv2d
# UnknownError:  Failed to get convolution algorithm. This is probably because cuDNN failed to initialize, so try looking to see if a warning log message was printed above.
# it is because the cnDNN version you installed is not compatible with the cuDNN version that compiled in tensorflow. -> Let conda or pip automatically choose the right version of tensorflow and cudnn.
# or run out of graphics card RAM -> must set limit for GPU RAM. Splitting into 2 logical GPU with different RAM limit. By default, Tensorflow will use on the logical GPU: 0, the GPU: 1 will be used for training generator and discriminator models.

# gpu_devices = tf.config.experimental.list_physical_devices('GPU')
# for device in gpu_devices:
#     tf.config.experimental.set_memory_growth(device, True)


# https://www.tensorflow.org/guide/gpu#limiting_gpu_memory_growth
# https://leimao.github.io/blog/TensorFlow-cuDNN-Failure/
gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
  # Restrict TensorFlow to only allocate 1GB of memory on the first GPU
  try:
    # Currently, memory growth needs to be the same across GPUs
#     for gpu in gpus:
#       tf.config.experimental.set_memory_growth(gpu, True)
    tf.config.experimental.set_virtual_device_configuration(
        gpus[0],
        [
         tf.config.experimental.VirtualDeviceConfiguration(memory_limit=1024*0.15),
         tf.config.experimental.VirtualDeviceConfiguration(memory_limit=1024*5.45) # for Training
#          tf.config.experimental.VirtualDeviceConfiguration(memory_limit=1024*5) # for Testing
        ])
    logical_gpus = tf.config.experimental.list_logical_devices('GPU')
    print(len(gpus), "Physical GPUs,", len(logical_gpus), "Logical GPUs")
  except RuntimeError as e:
    # Virtual devices must be set before GPUs have been initialized
    print(e)


# In[2]:

train_30fps_dir = os.path.join(*['data', 'REDS_VTSR', 'train', 'train_30fps'])
# print(train_30fps_dir)


# In[3]:

train_30fps_dir = [os.path.join(train_30fps_dir, p) for p in os.listdir(train_30fps_dir)]
# print('Train 30fps', train_30fps_dir[:2])

# ## 1.2. Randomize Videos Paths

# In[7]:


# [train_30fps_dir, train_60fps_dir, val_30fps_dir, val_60fps_dir]
import random
random.shuffle(train_30fps_dir) # make the training dataset random
# random.shuffle(train_60fps_dir) # make the training dataset random


# ## 1.3. Get Image Paths

# In[9]:


train_image_30fps_paths = []
for video_path in train_30fps_dir:
    for x in os.listdir(video_path):
        train_image_30fps_paths.append(os.path.join(video_path, x))

# output format: [image1.png, image2.png,...]

# # 2. Loading Data

# ## 2.1. Train Dataset Pipeline


# In[13]:


hr_height = 360 // 2
hr_width = 640 // 2
scale = 2

lr_height = hr_height // scale
lr_width = hr_width // scale

batch_size = 9

data_loader = DataLoader(hr_height, hr_width, lr_height, lr_width, batch_size)

train_dataset = data_loader.train_dataset(train_image_30fps_paths)

# # 3. Models

# ## 3.1. Generator Model

# In[20]:


hr_shape = (hr_height, hr_width, 3)
lr_shape = (lr_height, lr_width, 3)


# In[21]:


# We use a pre-trained VGG19 model to extract image features from the high resolution
# and the generated high resolution images and minimize the mse between them
# Get the vgg network. Extract features from Block 5, last convolution, exclude layer block5_pool (MaxPooling2D)
vgg = tf.keras.applications.VGG19(weights="imagenet", input_shape=hr_shape, include_top=False)
vgg.trainable = False

# Create model and compile
vgg_model = tf.keras.models.Model(inputs=vgg.input, outputs=vgg.get_layer("block5_conv4").output)
# vgg_model.summary()


# In[22]:


@tf.function
def feature_loss(hr, sr):
    """
    Returns Mean Square Error of VGG19 feature extracted original image (y) and VGG19 feature extracted generated image (y_hat).
    Args:
        hr: A tf tensor of original image (y)
        sr: A tf tensor of generated image (y_hat)
    Returns:
        mse: Mean Square Error.
    """
    sr = tf.keras.applications.vgg19.preprocess_input(((sr + 1.0) * 255) / 2.0)
    hr = tf.keras.applications.vgg19.preprocess_input(((hr + 1.0) * 255) / 2.0)
    sr_features = vgg_model(sr) / 12.75
    hr_features = vgg_model(hr) / 12.75
    mse = tf.keras.losses.MeanSquaredError()(hr_features, sr_features)
    return mse

@tf.function
def content_loss(hr, sr):
    """
    Returns Mean Square Error of original image (y) and generated image (y_hat).
    Args:
        hr: A tf tensor of original image (y)
        sr: A tf tensor of generated image (y_hat)
    Returns:
        mse: Mean Square Error.
    """
    sr = 255.0 * (sr + 1.0) / 2.0
    hr = 255.0 * (hr + 1.0) / 2.0
    mse = tf.keras.losses.MeanAbsoluteError()(sr, hr)
    return mse


# In[23]:


def build_generator():
    """Build the generator that will do the Super Resolution task.
    Based on the Mobilenet design. Idea from Galteri et al."""

    def make_divisible(v, divisor, min_value=None):
            if min_value is None:
                min_value = divisor
            new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
            # Make sure that round down does not go down by more than 10%.
            if new_v < 0.9 * v:
                new_v += divisor
            return new_v

    def residual_block(inputs, filters, block_id, expansion=6, stride=1, alpha=1.0):
        """Inverted Residual block that uses depth wise convolutions for parameter efficiency.
        Args:
            inputs: The input feature map.
            filters: Number of filters in each convolution in the block.
            block_id: An integer specifier for the id of the block in the graph.
            expansion: Channel expansion factor.
            stride: The stride of the convolution.
            alpha: Depth expansion factor.
        Returns:
            x: The output of the inverted residual block.
        """
        channel_axis = 1 if keras.backend.image_data_format() == 'channels_first' else -1

        in_channels = keras.backend.int_shape(inputs)[channel_axis]
        pointwise_conv_filters = int(filters * alpha)
        pointwise_filters = make_divisible(pointwise_conv_filters, 8)
        x = inputs
        prefix = 'block_{}_'.format(block_id)

        if block_id:
            # Expand
            x = keras.layers.Conv2D(expansion * in_channels, kernel_size=1, padding='same', use_bias=True, activation=None,
                                    name=prefix + 'expand')(x)
            x = keras.layers.BatchNormalization(axis=channel_axis, epsilon=1e-3, momentum=0.999,
                                                name=prefix + 'expand_BN')(x)
            x = keras.layers.Activation('relu', name=prefix + 'expand_relu')(x)
        else:
            prefix = 'expanded_conv_'

        # Depthwise
        x = keras.layers.DepthwiseConv2D(kernel_size=3, strides=stride, activation=None, use_bias=True, padding='same' if stride == 1 else 'valid',
                                         name=prefix + 'depthwise')(x)
        x = keras.layers.BatchNormalization(axis=channel_axis, epsilon=1e-3, momentum=0.999,
                                            name=prefix + 'depthwise_BN')(x)

        x = keras.layers.Activation('relu', name=prefix + 'depthwise_relu')(x)

        # Project
        x = keras.layers.Conv2D(pointwise_filters, kernel_size=1, padding='same', use_bias=True, activation=None,
                                name=prefix + 'project')(x)
        x = keras.layers.BatchNormalization(axis=channel_axis, epsilon=1e-3, momentum=0.999,
                                            name=prefix + 'project_BN')(x)

        if in_channels == pointwise_filters and stride == 1:
            return keras.layers.Add(name=prefix + 'add')([inputs, x])
        return x

    def deconv2d(layer_input):
        """Upsampling layer to increase height and width of the input.
        Uses Conv2DTranspose for upsampling.
        Args:
            layer_input: The input tensor to upsample.
        Returns:
            u: Upsampled input by a factor of 2.
        """
        
        u = tf.keras.layers.Conv2DTranspose(32, kernel_size=3, strides=2, padding="SAME")(layer_input)
        
        u = keras.layers.PReLU(shared_axes=[1, 2])(u)
        return u

    # Low resolution image input
    img_lr = keras.Input(shape=lr_shape)

    # Pre-residual block
    c1 = keras.layers.Conv2D(32, kernel_size=3, strides=1, padding='same')(img_lr)
    c1 = keras.layers.BatchNormalization()(c1)
    c1 = keras.layers.PReLU(shared_axes=[1, 2])(c1)

    # Propogate through residual blocks
    r = residual_block(c1, 32, 0)
    
    # Number of inverted residual blocks in the mobilenet generator    
    for idx in range(1, 6):
        r = residual_block(r, 32, idx)

    # Post-residual block
    c2 = keras.layers.Conv2D(32, kernel_size=3, strides=1, padding='same')(r)
    c2 = keras.layers.BatchNormalization()(c2)
    c2 = keras.layers.Add()([c2, c1])

    # Upsampling only 2 times
    u1 = deconv2d(c2)
    # u2 = deconv2d(u1)

    # Generate high resolution output
    gen_hr = keras.layers.Conv2D(3, kernel_size=3, strides=1, padding='same', activation='tanh')(u1)

    return keras.models.Model(img_lr, gen_hr)


# In[24]:

gen_model = build_generator()

gen_model.summary()

# ## 3.2. Discriminator Model

# In[26]:


def build_discriminator():
    """Builds a discriminator network based on the SRGAN design."""

    def d_block(layer_input, filters, strides=1, bn=True):
        """Discriminator layer block.
        Args:
            layer_input: Input feature map for the convolutional block.
            filters: Number of filters in the convolution.
            strides: The stride of the convolution.
            bn: Whether to use batch norm or not.
        """
        d = keras.layers.Conv2D(filters, kernel_size=3, strides=strides, padding='same')(layer_input)
        if bn:
            d = keras.layers.BatchNormalization(momentum=0.8)(d)
        d = keras.layers.LeakyReLU(alpha=0.2)(d)

        return d

    # Input img
    d0 = keras.layers.Input(shape=hr_shape)

    d1 = d_block(d0, 32, bn=False)
    d2 = d_block(d1, 32, strides=2)
    d3 = d_block(d2, 32)
    d4 = d_block(d3, 32, strides=2)
    d5 = d_block(d4, 64)
    d6 = d_block(d5, 64, strides=2)
    d7 = d_block(d6, 64)
    d8 = d_block(d7, 64, strides=2)

    validity = keras.layers.Conv2D(1, kernel_size=1, strides=1, activation='sigmoid', padding='same')(d8)

    return keras.models.Model(d0, validity)


# In[27]:


disc_model = build_discriminator()
disc_model.summary()


# ## 3.3. Optimizers

# In[28]:


# Define a learning rate decay schedule.
lr = 1e-3 
# * 0.95 ** ((10 * 1200) // 100000)
# print(lr)

gen_schedule = keras.optimizers.schedules.ExponentialDecay(
    lr,
    decay_steps=100000,
    decay_rate=0.95, # 95%
    staircase=True
)

disc_schedule = keras.optimizers.schedules.ExponentialDecay(
    lr * 5,  # TTUR - Two Time Scale Updates
    decay_steps=100000,
    decay_rate=0.95, # 95%
    staircase=True
)

gen_optimizer = keras.optimizers.Adam(learning_rate=gen_schedule)
disc_optimizer = keras.optimizers.Adam(learning_rate=disc_schedule)


# # 4. Training

# In[29]:

for layer in disc_model.layers:
    disc_model_output_shape = layer.output_shape
    # (None, 12, 20, 1)

# 23, 40, 1
height_patch = disc_model_output_shape[1]
# int(hr_height / 2 ** 4)

width_patch = disc_model_output_shape[2]
# int(hr_width / 2 ** 4)

disc_patch = (height_patch, width_patch, 1)
# disc_patch

pretrain_iteration = 1
train_iteration = 1


# In[30]:


@tf.function
def pretrain_step(gen_model, x, y):
    """
    Single step of generator pre-training.
    Args:
        gen_model: A compiled generator model.
        x: The low resolution image tensor.
        y: The high resolution image tensor.
    """
    with tf.GradientTape() as tape:
        fake_hr = gen_model(x)
        loss_mse = tf.keras.losses.MeanSquaredError()(y, fake_hr)

    grads = tape.gradient(loss_mse, gen_model.trainable_variables)
    gen_optimizer.apply_gradients(zip(grads, gen_model.trainable_variables))

    return loss_mse


def pretrain_generator(gen_model, dataset, writer, log_iter=200):
    """Function that pretrains the generator slightly, to avoid local minima.
    Args:
        gen_model: A compiled generator model.
        dataset: A tf dataset object of low and high res images to pretrain over.
        writer: A summary writer object.
    Returns:
        None
    """
    global pretrain_iteration

    with writer.as_default():
        for _ in range(1):
            for x, y in dataset:
                loss = pretrain_step(gen_model, x, y)
                if pretrain_iteration % log_iter == 0:
                    print(f'Pretrain Step: {pretrain_iteration}, Pretrain MSE Loss: {loss}')
                    tf.summary.scalar('MSE Loss', loss, step=tf.cast(pretrain_iteration, tf.int64))
                    writer.flush()
                pretrain_iteration += 1

@tf.function
def train_step(gen_model, disc_model, x, y):
    """Single train step function for the SRGAN.
    Args:
        gen_model: A compiled generator model.
        disc_model: A compiled discriminator model.
        x: The low resolution input image.
        y: The desired high resolution output image.
    Returns:
        disc_loss: The mean loss of the discriminator.
        adv_loss: The Binary Crossentropy loss between real label and predicted label.
        cont_loss: The Mean Square Error of VGG19 feature extracted original image (y) and VGG19 feature extractedgenerated image (y_hat).
        mse_loss: The Mean Square Error of original image (y) and generated image (y_hat).
    """
    # Label smoothing for better gradient flow
    valid = tf.ones((x.shape[0],) + disc_patch)
    fake = tf.zeros((x.shape[0],) + disc_patch)
#     print('label')
    with tf.GradientTape() as gen_tape, tf.GradientTape() as disc_tape:
        # From low res. image generate high res. version
        fake_hr = gen_model(x)
#         print('gen_model')

        # Train the discriminators (original images = real / generated = Fake)
        valid_prediction = disc_model(y)
        fake_prediction = disc_model(fake_hr)
#         print('disc_model')
        # Generator loss
        feat_loss = feature_loss(y, fake_hr)

        # not helping because it makes adversial loss increase and discriminator loss decrease
        # cont_loss = content_loss(y, fake_hr)

        # Adversarial Loss need to be decreased. Smallen the number to make it decrease faster
        adv_loss = 1e-3 * tf.keras.losses.BinaryCrossentropy()(valid, fake_prediction)
        mse_loss = tf.keras.losses.MeanSquaredError()(y, fake_hr)
        perceptual_loss = feat_loss + adv_loss + mse_loss

        # Discriminator loss
        valid_loss = tf.keras.losses.BinaryCrossentropy()(valid, valid_prediction)
        fake_loss = tf.keras.losses.BinaryCrossentropy()(fake, fake_prediction)
        disc_loss = tf.add(valid_loss, fake_loss)

#         print('finish gradient')
        
    # Backprop on Generator
    gen_grads = gen_tape.gradient(perceptual_loss, gen_model.trainable_variables)
    gen_optimizer.apply_gradients(zip(gen_grads, gen_model.trainable_variables))

    # Backprop on Discriminator
    disc_grads = disc_tape.gradient(disc_loss, disc_model.trainable_variables)
    disc_optimizer.apply_gradients(zip(disc_grads, disc_model.trainable_variables))
#     print('optimizer')
    
    return disc_loss, adv_loss, feat_loss, mse_loss


def train(gen_model, disc_model, dataset, writer, log_iter=200):
    """
    Function that defines a single training step for the SR-GAN.
    Args:
        gen_model: A compiled generator model.
        disc_model: A compiled discriminator model.
        dataset: A tf data object that contains low and high res images.
        log_iter: Number of iterations after which to add logs in 
                  tensorboard.
        writer: Summary writer
    """
    global train_iteration

    with writer.as_default():
        # Iterate over dataset
        for x, y in dataset:
            disc_loss, adv_loss, feat_loss, mse_loss = train_step(gen_model, disc_model, x, y)
#             print(train_iteration)
            # Log tensorboard summaries if log iteration is reached.
            if train_iteration % log_iter == 0:
                print(f'Train Step: {train_iteration}, Adversarial Loss: {adv_loss}, Feature Loss: {feat_loss}, MSE Loss: {mse_loss}, Discriminator Loss: {disc_loss}')
                
                tf.summary.scalar('Adversarial Loss', adv_loss, step=train_iteration)
                tf.summary.scalar('Feature Loss', feat_loss, step=train_iteration)
                # tf.summary.scalar('Content Loss', cont_loss, step=train_iteration)
                tf.summary.scalar('MSE Loss', mse_loss, step=train_iteration)
                tf.summary.scalar('Discriminator Loss', disc_loss, step=train_iteration)

                if train_iteration % (log_iter*10) == 0:
                    tf.summary.image('Low Res', tf.cast(255 * x, tf.uint8), step=train_iteration)
                    tf.summary.image('High Res', tf.cast(255 * (y + 1.0) / 2.0, tf.uint8), step=train_iteration)
                    tf.summary.image('Generated', tf.cast(255 * (gen_model.predict(x) + 1.0) / 2.0, tf.uint8), step=train_iteration)

                gen_model.save('models/generator_upscale_2_times.h5')
                disc_model.save('models/discriminator_upscale_2_times.h5')
                writer.flush()
            train_iteration += 1


# In[31]:

# New Training

with tf.device('/device:GPU:1'):

    # Define the directory for saving pretrainig loss tensorboard summary.
    pretrain_summary_writer = tf.summary.create_file_writer('upscale_2_times_logs/pretrain')
    
    # sample_train_dataset = dataset(train_image_30fps_paths[:180], batch_size=batch_size)

    # Run pre-training.
#     sample_train_dataset
#     train_dataset
    pretrain_generator(gen_model, train_dataset, pretrain_summary_writer, log_iter=200)
    gen_model.save('models/generator_upscale_2_times.h5')
    
    # Define the directory for saving the SRGAN training tensorbaord summary.
    train_summary_writer = tf.summary.create_file_writer('upscale_2_times_logs/train')

    epochs = 10 # Turn on Turbo mode
    # speed: 14 min/epoch

    # training history: 
    # 10 epochs (first): 2 hours
    
# Run training.
for _ in range(epochs):
    print('===================')
    print(f'Epoch: {_}\n')

    # shuffle video directories
    # [train_30fps_dir, train_60fps_dir, val_30fps_dir, val_60fps_dir]
    random.shuffle(train_30fps_dir) # make the training dataset random

    train_image_30fps_paths = []
    for video_path in train_30fps_dir:
        for x in os.listdir(video_path):
            train_image_30fps_paths.append(os.path.join(video_path, x))

    # recreate dataset every epoch to lightly augment the frames. ".repeat()" in dataset pipeline function does not help.
    train_dataset = data_loader.train_dataset(train_image_30fps_paths)
    # sample_train_dataset = data_loader.train_dataset(train_image_30fps_paths[:180])

    with tf.device('/device:GPU:1'):
        train(gen_model, disc_model, train_dataset, train_summary_writer, log_iter=200)
        
# import os
import time
time.sleep(10)
os.system('shutdown /p /f')