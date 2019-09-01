import json
import math
import os
import sys
from glob import glob
from typing import Tuple, Dict, Any

import torch
import torch.nn.functional as F
import utils
from PIL import Image
from PIL.ImageDraw import ImageDraw
from torch.nn import Module, Conv2d, MaxPool2d, Linear, AdaptiveAvgPool2d, ReLU6, MSELoss, L1Loss
from torch.utils.data import Dataset
from torch.utils.data.dataloader import DataLoader
from torchsummary import summary
import numpy as np
from torchvision.transforms import ToTensor


class BoundingBoxRefinementDataset(Dataset):
    """ A dataset, dedicated to bounding box refinement

    The only specificity that we require is that the dataset `__getitem__` should return:

    * image: a PIL Image of size (H, W) that contains just one stave
    * target: a dict containing the following fields
        * `boxes` (`FloatTensor[N, 4]`): the coordinates of the `N` bounding boxes in `[x0, y0, x1, y1]` format, ranging from `0` to `W` and `0` to `H`
        * `labels` (`Int64Tensor[N]`): the label for each bounding box
        * `image_id` (`Int64Tensor[1]`): an image identifier. It should be unique between all the images in the dataset, and is used during evaluation
        * `area` (`Tensor[N]`): The area of the bounding box. This is used during evaluation with the COCO metric, to separate the metric scores between small, medium and large boxes.
        * `iscrowd` (`UInt8Tensor[N]`): instances with `iscrowd=True` will be ignored during evaluation.
        * (optionally) `masks` (`UInt8Tensor[N, H, W]`): The segmentation masks for each one of the objects
        * (optionally) `keypoints` (`FloatTensor[N, K, 3]`): For each one of the `N` objects, it contains the `K` keypoints in `[x, y, visibility]` format, defining the object. `visibility=0` means that the keypoint is not visible. Note that for data augmentation, the notion of flipping a keypoint is dependent on the data representation, and you should probably adapt `references/detection/transforms.py` for your new keypoint representation

    If your model returns the above methods, they will make it work for both training and evaluation, and will use the evaluation scripts from pycocotools.

    Additionally, if you want to use aspect ratio grouping during training (so that each batch only contains images with similar aspect ratio), then it is recommended to also implement a `get_height_and_width` method, which returns the height and the width of the image. If this method is not provided, we query all elements of the dataset via `__getitem__` , which loads the image in memory and is slower than if a custom method is provided.

    """

    def __init__(self, data_directory: str, margin_around_stave=100, transforms=None):
        self.data_directory = data_directory
        self.margin_around_stave = margin_around_stave
        self.transforms = transforms
        # load all image files, sorting them to ensure that they are aligned
        images = sorted(glob(data_directory + "/*.png") + glob(data_directory + "/*.jpg"))
        annotation_files = sorted(glob(data_directory + "/*.json"))

        self.dataset = []

        for annotation_file, image_file in zip(annotation_files, images):
            annotations = self.load_annotations(annotation_file)
            for stave in annotations["staves"]:
                self.dataset.append((image_file, stave))

    def __getitem__(self, index: int) -> Tuple[Image.Image, Dict[str, float]]:
        image_path, bounding_box = self.dataset[index]
        image = self.load_image(image_path, bounding_box)

        crop_top = max(0, bounding_box["top"] - self.margin_around_stave)
        crop_bottom = min(image.height, bounding_box["bottom"] + self.margin_around_stave)

        cropped_image = image.crop([0, crop_top, image.width, crop_bottom])  # Crop only top and bottom
        bounding_box["top"] = bounding_box["top"] - crop_top
        bounding_box["bottom"] = bounding_box["bottom"] - crop_top

        # image_draw = ImageDraw(cropped_image)
        # image_draw.rectangle([int(bounding_box['left']), int(bounding_box['top']), int(bounding_box['right']),
        #                       int(bounding_box['bottom'])],
        #                      outline='#008888', width=2)

        cropped_image = ToTensor()(cropped_image)

        left, top, right, bottom = bounding_box["left"], bounding_box["top"], bounding_box["right"], bounding_box[
            "bottom"]
        width = right - left
        height = bottom - top
        center_x = left + width / 2
        center_y = top + height / 2

        boxes = torch.as_tensor([center_x, center_y, width, height], dtype=torch.float32)

        if self.transforms is not None:
            cropped_image, boxes = self.transforms(cropped_image, boxes)

        return cropped_image, boxes

    def load_image(self, image_path, bounding_box) -> Image.Image:
        image = Image.open(image_path).convert("RGB")
        return image

    def __len__(self) -> int:
        return len(self.dataset)

    def load_annotations(self, annotation_file):
        with open(annotation_file, 'r') as gt_file:
            annotations = json.load(gt_file)
        return annotations


class DetectionRefinementModel(Module):

    def __init__(self):
        super(DetectionRefinementModel, self).__init__()
        self.pool = MaxPool2d(2, 2)
        self.conv1 = Conv2d(3, 64, 3, padding=1)
        self.conv2 = Conv2d(64, 96, 3, padding=1)
        self.conv3 = Conv2d(96, 128, 3, padding=1)
        self.conv4 = Conv2d(128, 192, 3, padding=1)
        self.avg_pool = AdaptiveAvgPool2d((1, 1))
        self.linear1 = Linear(192, 64)
        self.linear2 = Linear(64, 4)
        self.relu = ReLU6(inplace=True)

    def forward(self, image, unrefined_bounding_box):
        x = self.pool(self.relu(self.conv1(image)))
        x = self.pool(self.relu(self.conv2(x)))
        x = self.pool(self.relu(self.conv3(x)))
        x = self.relu(self.conv4(x))

        x = self.avg_pool(x)
        x = x.view(x.size(0), -1)
        x = self.relu(self.linear1(x))
        x = self.relu(self.linear2(x))
        # x = x + unrefined_bounding_box

        return x


def train_one_epoch(model, optimizer, data_loader, device, epoch, print_freq):
    model.train()
    criterion = L1Loss()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)

    lr_scheduler = None
    # if epoch == 0:
    #     warmup_factor = 1. / 1000
    #     warmup_iters = min(1000, len(data_loader) - 1)
    #
    #     lr_scheduler = utils.warmup_lr_scheduler(optimizer, warmup_iters, warmup_factor)

    for images, targets in metric_logger.log_every(data_loader, print_freq, header):
        images = images.to(device)
        targets = targets.to(device)

        prediction = model(images, None)
        loss = criterion(prediction, targets)

        if not math.isfinite(loss):
            print("Loss is {}, stopping training".format(loss))
            sys.exit(1)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        if lr_scheduler is not None:
            lr_scheduler.step()

        metric_logger.update(loss=loss)
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])


if __name__ == '__main__':
    device = "cpu"  # torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model = DetectionRefinementModel()
    print(model)
    # If you get an error, go to torchsummary.py and fix line 100 into
    # total_input_size = abs(np.prod(sum(input_size,())) * batch_size * 4. / (1024 ** 2.))
    # see https://github.com/sksq96/pytorch-summary/issues/90
    summary(model, [(3, 128, 256), (4,)], device=device)
    model.to(device)

    dataset = BoundingBoxRefinementDataset("D:\Dropbox\Stave Detection\CVCMUSCIMA_2000")
    first_image, bounding_box = dataset[0]
    # first_image.show()
    training_dataset_loader = DataLoader(dataset, batch_size=1, shuffle=True)

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.Adam(params, lr=0.001, weight_decay=0.0005)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=10000, gamma=0.5)

    num_epochs = 10

    for epoch in range(num_epochs):
        # train for one epoch, printing every 10 iterations
        train_one_epoch(model, optimizer, training_dataset_loader, device, epoch, print_freq=1)
        # update the learning rate
        lr_scheduler.step()
        # evaluate on the test dataset
        # evaluate(model, data_loader_test, device=device)
        torch.save(model.state_dict(), "model-{0}.pth".format(epoch))
