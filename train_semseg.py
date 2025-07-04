import argparse
import datetime
import importlib
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from tqdm import tqdm

import numpy as np
import torch

from data_utils.S3DISDataLoader import S3DISDataset

sys.path.append("D:/Git/smpp-3D-model-comparator/src/nn")
from data_preparation import Compose, ShufflePoints, JitterPoints, RotationPoints, ScalePoints

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = BASE_DIR

sys.path.append(os.path.join(ROOT_DIR, 'models'))

transforms = Compose([
                ShufflePoints(),
                JitterPoints(sigma=0.01, clip=0.02),
                RotationPoints(angle_range=(0, 90)),
                ScalePoints(scale_range=(0.8, 1.2))])

classes = ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13', '14']
class2label = {cls: i for i, cls in enumerate(classes)} # -> {class_name: index}
seg_classes = class2label
seg_label_to_cat = {}
for i, cat in enumerate(seg_classes.keys()):
    seg_label_to_cat[i] = cat # -> {index: class_name}

# --- ??? ---
def inplace_relu(m):
    classname = m.__class__.__name__
    if classname.find('ReLU') != -1:
        m.inplace=True

# Инициализация весов перед началом обучения.
def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv2d') != -1:
        torch.nn.init.xavier_normal_(m.weight.data)
        torch.nn.init.constant_(m.bias.data, 0.0)
    elif classname.find('Linear') != -1:
        torch.nn.init.xavier_normal_(m.weight.data)
        torch.nn.init.constant_(m.bias.data, 0.0)

# Изменяет параметр momentum у слоёв BatchNorm1d и BatchNorm2d.
def bn_momentum_adjust(m, momentum):
    if isinstance(m, torch.nn.BatchNorm2d) or isinstance(m, torch.nn.BatchNorm1d):
        m.momentum = momentum

def worker_init_fn(worker_id):
        np.random.seed(worker_id + int(time.time()))

def parse_args():
    parser = argparse.ArgumentParser('Model')
    parser.add_argument('--model', type=str, default='pointnet_sem_seg',
        help='model name [default: pointnet_sem_seg]')
    parser.add_argument('--batch_size', type=int, default=8,
        help='Batch Size during training [default: 16]')
    parser.add_argument('--epoch', default=1, type=int,
        help='Epoch to run [default: 32]')
    parser.add_argument('--learning_rate', default=0.001, type=float,
        help='Initial learning rate [default: 0.001]')
    parser.add_argument('--gpu', type=str, default='0',
        help='GPU to use [default: GPU 0]')
    parser.add_argument('--optimizer', type=str, default='Adam',
        help='Adam or SGD [default: Adam]')
    parser.add_argument('--log_dir', type=str, default=None,
        help='Log path [default: None]')
    parser.add_argument('--decay_rate', type=float, default=1e-4,
        help='Weight decay [default: 1e-4]')
    parser.add_argument('--npoint', type=int, default=16384,
        help='Point Number [default: 4096]')
    parser.add_argument('--step_size', type=int, default=10,
        help='Decay step for lr decay [default: every 10 epochs]')
    parser.add_argument('--lr_decay', type=float, default=0.7,
        help='Decay rate for lr decay [default: 0.7]')
    parser.add_argument('--test_area', type=int, default=5, 
        help='Which area to use for test, option: 1-6 [default: 5]')
    return parser.parse_args()


def main(args):

    NUM_CLASSES = 15
    NUM_POINT = args.npoint
    BATCH_SIZE = args.batch_size
    LEARNING_RATE_CLIP = 1e-5
    MOMENTUM_ORIGINAL = 0.1
    MOMENTUM_DECCAY = 0.5
    MOMENTUM_DECCAY_STEP = args.step_size

    '''HYPER PARAMETER'''
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

    '''CREATE DIR'''
    timestr = str(datetime.datetime.now().strftime('%Y-%m-%d_%H-%M'))
    experiment_dir = Path('./log/')
    experiment_dir.mkdir(exist_ok=True)
    experiment_dir = experiment_dir.joinpath('sem_seg')
    experiment_dir.mkdir(exist_ok=True)
    if args.log_dir is None:
        experiment_dir = experiment_dir.joinpath(timestr)
    else:
        experiment_dir = experiment_dir.joinpath(args.log_dir)
    experiment_dir.mkdir(exist_ok=True)
    checkpoints_dir = experiment_dir.joinpath('checkpoints/')
    checkpoints_dir.mkdir(exist_ok=True)
    log_dir = experiment_dir.joinpath('logs/')
    log_dir.mkdir(exist_ok=True)

    '''LOG'''
    logger = logging.getLogger("Model")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler = logging.FileHandler('%s/%s.txt' % (log_dir, args.model))
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    def log_string(str):
        logger.info(str)
        print(str)

    log_string(' ')
    log_string(' ')
    log_string('PARAMETER ...')
    log_string(args)

    train_root = 'data/stanford_indoor3d/train'
    # valid_root = 'data/stanford_indoor3d/valid'

    print("start loading training data ...")
    TRAIN_DATASET = S3DISDataset(
                        split='train',
                        data_root=train_root,
                        num_point=NUM_POINT,
                        test_area=args.test_area,
                        block_size=0.01,
                        sample_rate=1.0,
                        transform=transforms)
    trainDataLoader = torch.utils.data.DataLoader(
                        TRAIN_DATASET,
                        batch_size=BATCH_SIZE,
                        shuffle=True,
                        num_workers=0,
                        pin_memory=False,
                        drop_last=True,
                        worker_init_fn=worker_init_fn)

    print("start loading test data ...")
    TEST_DATASET = S3DISDataset(
                        split='test',
                        data_root=train_root,
                        num_point=NUM_POINT,
                        test_area=args.test_area,
                        block_size=0.01,
                        sample_rate=1.0,
                        transform=None)
    testDataLoader = torch.utils.data.DataLoader(
                        TEST_DATASET,
                        batch_size=BATCH_SIZE,
                        shuffle=False,
                        num_workers=0,
                        pin_memory=False,
                        drop_last=True)
    
    # Веса для функции потерь.
    weights = torch.Tensor(TRAIN_DATASET.labelweights).cuda()

    log_string("The number of training data is: %d" % len(TRAIN_DATASET))
    log_string("The number of test data is: %d" % len(TEST_DATASET))

    '''MODEL LOADING'''
    MODEL = importlib.import_module(args.model)
    shutil.copy('models/%s.py' % args.model, str(experiment_dir))

    classifier = MODEL.get_model(NUM_CLASSES).cuda()
    criterion = MODEL.get_loss().cuda()
    classifier.apply(inplace_relu)

    # Дообучение модели при наличии best_model.pth.
    try:
        checkpoint = torch.load(str(experiment_dir) + '/checkpoints/best_model.pth')
        start_epoch = checkpoint['epoch']
        classifier.load_state_dict(checkpoint['model_state_dict'])
        log_string('Use pretrain model')
    # Инициализация весов при помощи weights_init, если best_model.pth отсутсвует.
    except:
        log_string('No existing model, starting training from scratch...')
        start_epoch = 0
        classifier = classifier.apply(weights_init)
    
    # Выбор опитимизатора (Adam или SGD). 
    if args.optimizer == 'Adam':
        optimizer = torch.optim.Adam(
            classifier.parameters(),
            lr=args.learning_rate,
            betas=(0.9, 0.999),
            eps=1e-08,
            weight_decay=args.decay_rate)
    else:
        optimizer = torch.optim.SGD(classifier.parameters(), lr=args.learning_rate, momentum=0.9)
    
    best_iou = 0
    global_epoch = 0

    for epoch in range(start_epoch, args.epoch):
        '''Train on chopped scenes'''
        log_string(' ')
        log_string('**** Epoch %d (%d/%s) ****' % (global_epoch + 1, epoch + 1, args.epoch))
        # Обновление learning rate каждые 10 эпох (args.step_size).
        lr = max(
            args.learning_rate * (args.lr_decay ** (epoch // args.step_size)), LEARNING_RATE_CLIP)
        log_string('Learning rate:%f' % lr)
        # Присвоение нового lr всем параметрам оптимизатора.
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
        # Обновление momentum каждые 10 эпох (args.step_size).
        momentum = MOMENTUM_ORIGINAL * (MOMENTUM_DECCAY ** (epoch // MOMENTUM_DECCAY_STEP))
        if momentum < 0.01:
            momentum = 0.01
        print('BN momentum updated to: %f' % momentum)
        # Применение нового momentum к BatchNorm-слоям.
        classifier = classifier.apply(lambda x: bn_momentum_adjust(x, momentum))
        
        num_batches = len(trainDataLoader)
        total_correct = 0  # Общее количество верно предсказанных точек.
        total_seen = 0     # Общее количество точек по всем батчам.
        loss_sum = 0       # Суммма ошибок по всем батчам на train.
        classifier = classifier.train()

        for i, (points, target) in tqdm(
            enumerate(trainDataLoader), total=len(trainDataLoader), smoothing=0.9
            ):
            optimizer.zero_grad()
            points = points.float().cuda()
            points = points.transpose(2, 1) # [B, N, C] -> [B, C, N]
            target = target.long().cuda() # -> [B, N]
            # Получаем предсказания классификатора.
            seg_pred, trans_feat = classifier(points)
            seg_pred = seg_pred.contiguous().view(-1, NUM_CLASSES) # -> torch.tensor[B*N, NUM_CLASSES]
            batch_label = target.view(-1, 1)[:, 0].cpu().data.numpy() # -> np.ndarray[B*N]
            target = target.view(-1, 1)[:, 0] # -> torch.tensor[B*N]
            # Подсчет ошибки, обратное распространение, шаг оптимизатора.
            loss = criterion(seg_pred, target, trans_feat, weights)
            loss.backward()
            optimizer.step()
            # Подсчет loss и accuracy.
            pred_choice = seg_pred.cpu().data.max(1)[1].numpy() # -> np.ndarray[B*N]
            correct = np.sum(pred_choice == batch_label) # Сумма верно предсказанных точек в батче.
            total_correct += correct # Общее количество верно предсказанных точек.
            total_seen += (BATCH_SIZE * NUM_POINT) # Общее количество точек по всем батчам.
            loss_sum += loss # Суммма ошибок по всем батчам.

        # print("seg_pred: ", type(seg_pred), seg_pred.shape)
        # print("batch_label: ", type(batch_label), batch_label.shape)
        # print("target: ", type(target), target.shape)
        # print("pred_choice: ", type(pred_choice), pred_choice.shape)

        # Среднее по батчам значение ошибки. 
        log_string('Training mean loss: %f' % (loss_sum / num_batches))
        log_string('Training accuracy: %f' % (total_correct / float(total_seen)))

        # Сохранение весов через каждые 5 эпох.
        if epoch % 5 == 0:
            logger.info('Save model...')
            savepath = str(checkpoints_dir) + '/model.pth'
            log_string('Saving at %s' % savepath)
            state = {
                'epoch': epoch,
                'model_state_dict': classifier.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
            }
            torch.save(state, savepath)
            log_string('Saving model....')
    
        '''Evaluate on chopped scenes'''
        with torch.no_grad():
            num_batches = len(testDataLoader)
            total_correct = 0  # Общее количество верно предсказанных точек.
            total_seen = 0     # Общее количество точек по всем батчам.
            loss_sum = 0       # Суммма ошибок по всем батчам на train.
            classifier = classifier.eval()

            labelweights = np.zeros(NUM_CLASSES)
            # Реальное количество точек для каждого класса.
            total_seen_class = [0 for _ in range(NUM_CLASSES)] 
            # Предсказанное количество точек для каждого класса (числитель IoU).
            total_correct_class = [0 for _ in range(NUM_CLASSES)]
            # Объединение предсказанных и истинных точек (знаменатель IoU).
            total_iou_deno_class = [0 for _ in range(NUM_CLASSES)]
            
            # Тестирование на валидационной выборке.
            log_string('---- EPOCH %03d EVALUATION ----' % (global_epoch + 1))
            for i, (points, target) in tqdm(
                enumerate(testDataLoader), total=len(testDataLoader), smoothing=0.9
                ):
                points = points.float().cuda()
                points = points.transpose(2, 1)
                target = target.long().cuda()
                # Получаем предсказания классификатора на валидационной выборке.
                seg_pred, trans_feat = classifier(points) # -> _ , torch.tensor[B, 64, 64]
                seg_pred = seg_pred.contiguous().view(-1, NUM_CLASSES) # -> torch.tensor[B*N, NUM_CLASSES]
                batch_label = target.view(-1).cpu().data.numpy() # -> np.ndarray[B*N]
                target = target.view(-1, 1)[:, 0] # -> torch.tensor[B*N]
                # Подсчет ошибки на валидационной выборке.
                loss = criterion(seg_pred, target, trans_feat, weights)
                pred_choice = seg_pred.cpu().data.max(1)[1].numpy() # -> np.ndarray[B*N]
                correct = np.sum((pred_choice == batch_label))
                total_correct += correct # Общее количество верно предсказанных точек.
                total_seen += (BATCH_SIZE * NUM_POINT) # Общее количество точек по всем батчам.
                loss_sum += loss # Суммма ошибок по всем батчам.
            
                tmp, _ = np.histogram(batch_label, range(NUM_CLASSES + 1))
                labelweights += tmp

                for l in range(NUM_CLASSES):
                    total_seen_class[l] += np.sum((batch_label == l))
                    total_correct_class[l] += np.sum((pred_choice == l) & (batch_label == l))
                    total_iou_deno_class[l] += np.sum(((pred_choice == l) | (batch_label == l)))

            # print("seg_pred: ", type(seg_pred), seg_pred.shape)
            # print("trans_feat: ", type(trans_feat), trans_feat.shape)
            # print("batch_label: ", type(batch_label), batch_label.shape)
            # print("target: ", type(target), target.shape)
            # print("pred_choice: ", type(pred_choice), pred_choice.shape)

            labelweights = labelweights.astype(np.float32) / np.sum(labelweights.astype(np.float32))

            log_string('Eval mean loss: %f' % (loss_sum / float(num_batches)))
            # Общая (взвешенная) точность. Классы с большим числом точек имеют больший вклад.
            log_string('Eval accuracy: %f' % (total_correct / float(total_seen)))
            # Средняя (невзвешенная) точность. Одинаково учитывает каждый класс, даже если точек в нём мало.
            log_string('Eval avg class acc: %f' % (
                np.mean(np.array(total_correct_class) / (np.array(total_seen_class, dtype=np.float64) + 1e-6))))
            # Среднее по всем классам значение IoU.
            mIoU = np.mean(
                np.array(total_correct_class) / (np.array(total_iou_deno_class, dtype=np.float64) + 1e-6)
                )
            log_string('Eval avg class IoU: %f' % (mIoU))
            # IoU по каждому классу в отдельности.
            iou_per_class_str = '------- IoU --------\n'
            for l in range(NUM_CLASSES):
                iou_per_class_str += 'class %s weight: %.3f, IoU: %.3f \n' % (
                    seg_label_to_cat[l] + ' ' * (14 - len(seg_label_to_cat[l])), # Имя класса,
                    labelweights[l - 1],                                         # Вес класса,
                    total_correct_class[l] / float(total_iou_deno_class[l])      # IoU класса.
                    )
            log_string(iou_per_class_str)

            if mIoU >= best_iou:
                best_iou = mIoU
                logger.info('Save model...')
                savepath = str(checkpoints_dir) + '/best_model.pth'
                log_string('Saving at %s' % savepath)
                state = {
                    'epoch': epoch,
                    'class_avg_iou': mIoU,
                    'model_state_dict': classifier.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                }
                torch.save(state, savepath)
                log_string('Saving model....')
            log_string('Best mIoU: %f' % best_iou)
        global_epoch += 1


if __name__ == '__main__':
    args = parse_args()
    main(args)
