# Copyright 2019 Google LLC (original)
# Copyright 2019 Uizard Technologies (small modifications)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Mean teachers are better role models:
    Weight-averaged consistency targets improve semi-supervised deep learning results.

Reimplementation of https://arxiv.org/abs/1703.01780
"""
import functools
import os
import itertools


from absl import app
from absl import flags
from easydict import EasyDict
from libml import models, utils
from libml.data_pair import DATASETS, stack_augment
from libml.data import DataSet, augment_cifar10, augment_color, augment_cutout, augment_stl10, augment_svhn
import tensorflow as tf

FLAGS = flags.FLAGS


class MeanTeacher(models.MultiModel):

    def model(self, lr, wd, ema, warmup_pos, consistency_weight, **kwargs):
        hwc = [self.dataset.height, self.dataset.width, self.dataset.colors]
        x_in = tf.placeholder(tf.float32, [None] + hwc, 'x')
        y_in = tf.placeholder(tf.float32, [None, 2] + hwc, 'y')
        l_in = tf.placeholder(tf.int32, [None], 'labels')
        l = tf.one_hot(l_in, self.nclass)
        wd *= lr
        warmup = tf.clip_by_value(tf.to_float(self.step) / (warmup_pos * (FLAGS.train_kimg << 10)), 0, 1)

        classifier = functools.partial(self.classifier, **kwargs)
        logits_x = classifier(x_in, training=True)
        post_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)  # Take only first call to update batch norm.
        y = tf.reshape(tf.transpose(y_in, [1, 0, 2, 3, 4]), [-1] + hwc)
        y_1, y_2 = tf.split(y, 2)
        ema = tf.train.ExponentialMovingAverage(decay=ema)
        ema_op = ema.apply(utils.model_vars())
        ema_getter = functools.partial(utils.getter_ema, ema)
        logits_y = classifier(y_1, training=True, getter=ema_getter)
        logits_teacher = tf.stop_gradient(logits_y)
        logits_student = classifier(y_2, training=True)
        loss_mt = tf.reduce_mean((tf.nn.softmax(logits_teacher) - tf.nn.softmax(logits_student)) ** 2, -1)
        loss_mt = tf.reduce_mean(loss_mt)

        loss = tf.nn.softmax_cross_entropy_with_logits_v2(labels=l, logits=logits_x)
        loss = tf.reduce_mean(loss)
        tf.summary.scalar('losses/xe', loss)
        tf.summary.scalar('losses/mt', loss_mt)

        post_ops.append(ema_op)
        post_ops.extend([tf.assign(v, v * (1 - wd)) for v in utils.model_vars('classify') if 'kernel' in v.name])

        train_op = tf.train.AdamOptimizer(lr).minimize(loss + loss_mt * warmup * consistency_weight,
                                                       colocate_gradients_with_ops=True)
        with tf.control_dependencies([train_op]):
            train_op = tf.group(*post_ops)

        # Tuning op: only retrain batch norm.
        skip_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        classifier(x_in, training=True)
        train_bn = tf.group(*[v for v in tf.get_collection(tf.GraphKeys.UPDATE_OPS)
                              if v not in skip_ops])

        return EasyDict(
            x=x_in, y=y_in, label=l_in, train_op=train_op, tune_op=train_bn,
            classify_raw=tf.nn.softmax(classifier(x_in, training=False)),  # No EMA, for debugging.
            classify_op=tf.nn.softmax(classifier(x_in, getter=ema_getter, training=False)))


def get_dataset():
    assert FLAGS.dataset in DATASETS.keys() or FLAGS.custom_dataset, "Please enter a valid dataset name or use the --custom_dataset flag."

    # CIFAR10, CIFAR100, STL10, and SVHN are the pre-configured datasets
    # with each dataset's default augmentation.
    if FLAGS.dataset in DATASETS.keys():
        dataset = DATASETS[FLAGS.dataset]()

    # If the dataset has not been pre-configured, create it.
    else:
        label_size = [int(size) for size in FLAGS.label_size]
        valid_size = [int(size) for size in FLAGS.valid_size]

        augment_dict = {"cifar10": augment_cifar10, "cutout": augment_cutout, "svhn": augment_svhn, "stl10": augment_stl10, "color": augment_color}
        augmentation = augment_dict[FLAGS.augment]

        DATASETS.update([DataSet.creator(FLAGS.dataset.split(".")[0], seed, label, valid, [augmentation, stack_augment(augmentation)], \
            nclass=FLAGS.nclass, height=FLAGS.img_size, width=FLAGS.img_size, do_memoize=FLAGS.memoize)
                         for seed, label, valid in
                         itertools.product(range(2), label_size, valid_size)])
        dataset = DATASETS[FLAGS.dataset]()

    return dataset

def main(argv):
    del argv  # Unused.
    
    dataset = get_dataset()
    log_width = utils.ilog2(dataset.width)
    model = MeanTeacher(
        os.path.join(FLAGS.train_dir, dataset.name),
        dataset,
        lr=FLAGS.lr,
        wd=FLAGS.wd,
        arch=FLAGS.arch,
        warmup_pos=FLAGS.warmup_pos,
        batch=FLAGS.batch,
        nclass=dataset.nclass,
        ema=FLAGS.ema,
        smoothing=FLAGS.smoothing,
        consistency_weight=FLAGS.consistency_weight,
        scales=FLAGS.scales or (log_width - 2),
        filters=FLAGS.filters,
        repeat=FLAGS.repeat)

    
    model.train(FLAGS.train_kimg << 10, FLAGS.report_kimg << 10)


if __name__ == '__main__':
    utils.setup_tf()
    flags.DEFINE_float('consistency_weight', 50., 'Consistency weight.')
    flags.DEFINE_float('warmup_pos', 0.4, 'Relative position at which constraint loss warmup ends.')
    flags.DEFINE_float('wd', 0.2, 'Weight decay.')
    flags.DEFINE_float('ema', 0.999, 'Exponential moving average of params.')
    flags.DEFINE_float('smoothing', 0.001, 'Label smoothing.')
    flags.DEFINE_integer('scales', 0, 'Number of 2x2 downscalings in the classifier.')
    flags.DEFINE_integer('filters', 32, 'Filter size of convolutions.')
    flags.DEFINE_integer('repeat', 4, 'Number of residual layers per stage.')
    flags.DEFINE_bool('custom_dataset', True, 'True if using a custom dataset.')
    flags.DEFINE_integer('nclass', 10, 'Number of classes present in custom dataset.')
    flags.DEFINE_integer('img_size', 32, 'Size of Images in custom dataset')
    flags.DEFINE_spaceseplist('label_size', ['250', '1000', '2000', '4000'], 'List of different labeled data sizes.')
    flags.DEFINE_spaceseplist('valid_size', ['1', '500'], 'List of different validation sizes.')
    flags.DEFINE_string('augment', 'cifar10', 'Type of augmentation to use, as defined in libml.data.py')
    flags.DEFINE_bool('memoize', True, 'True if the dataset can be modified in memory.')
    FLAGS.set_default('dataset', 'cifar10.3@250-5000')
    FLAGS.set_default('batch', 64)
    FLAGS.set_default('lr', 0.002)
    FLAGS.set_default('train_kimg', 1 << 16)
    flags.register_validator('nu', lambda nu: nu == 2, message='nu must be 2 for pi-model.')
    app.run(main)
