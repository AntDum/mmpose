# Copyright (c) OpenMMLab. All rights reserved.

from copy import deepcopy
from typing import Optional, Tuple

import numpy as np

from mmpose.registry import KEYPOINT_CODECS
from .base import BaseKeypointCodec
from .utils import camera_to_image_coord


@KEYPOINT_CODECS.register_module()
class MotionBERTLabel(BaseKeypointCodec):
    r"""Generate keypoint and label coordinates for `MotionBERT`_ by Zhu et al
    (2022).

    Note:

        - instance number: N
        - keypoint number: K
        - keypoint dimension: D
        - pose-lifitng target dimension: C

    Args:
        num_keypoints (int): The number of keypoints in the dataset.
        root_index (int): Root keypoint index in the pose. Default: 0.
        remove_root (bool): If true, remove the root keypoint from the pose.
            Default: ``False``.
        save_index (bool): If true, store the root position separated from the
            original pose, only takes effect if ``remove_root`` is ``True``.
            Default: ``False``.
        concat_vis (bool): If true, concat the visibility item of keypoints.
            Default: ``False``.
        rootrel (bool): If true, the root keypoint will be set to the
            coordinate origin. Default: ``False``.
        factor_label (bool): If true, the label will be multiplied by a factor.
            Default: ``True``.
    """

    auxiliary_encode_keys = {
        'lifting_target', 'lifting_target_visible', 'camera_param', 'factor'
    }

    def __init__(self,
                 num_keypoints: int,
                 root_index: int = 0,
                 remove_root: bool = False,
                 save_index: bool = False,
                 concat_vis: bool = False,
                 rootrel: bool = False,
                 factor_label: bool = True):
        super().__init__()

        self.num_keypoints = num_keypoints
        self.root_index = root_index
        self.remove_root = remove_root
        self.save_index = save_index
        self.concat_vis = concat_vis
        self.rootrel = rootrel
        self.factor_label = factor_label

    def encode(self,
               keypoints: np.ndarray,
               keypoints_visible: Optional[np.ndarray] = None,
               lifting_target: Optional[np.ndarray] = None,
               lifting_target_visible: Optional[np.ndarray] = None,
               camera_param: Optional[dict] = None,
               factor: Optional[np.ndarray] = None) -> dict:
        """Encoding keypoints from input image space to normalized space.

        Args:
            keypoints (np.ndarray): Keypoint coordinates in shape (B, T, K, D).
            keypoints_visible (np.ndarray, optional): Keypoint visibilities in
                shape (B, T, K).
            lifting_target (np.ndarray, optional): 3d target coordinate in
                shape (T, K, C).
            lifting_target_visible (np.ndarray, optional): Target coordinate in
                shape (T, K, ).
            camera_param (dict, optional): The camera parameter dictionary.
            factor (np.ndarray, optional): The factor mapping camera and image
                  coordinate in shape (T, ).

        Returns:
            encoded (dict): Contains the following items:

                - keypoint_labels (np.ndarray): The processed keypoints in
                  shape like (N, K, D).
                - keypoint_labels_visible (np.ndarray): The processed
                  keypoints' weights in shape (N, K, ) or (N, K-1, ).
                - lifting_target_label: The processed target coordinate in
                  shape (K, C) or (K-1, C).
                - lifting_target_weights (np.ndarray): The target weights in
                  shape (K, ) or (K-1, ).
                - trajectory_weights (np.ndarray): The trajectory weights in
                  shape (K, ).
                - factor (np.ndarray): The factor mapping camera and image
                  coordinate in shape (T, 1).
        """
        if keypoints_visible is None:
            keypoints_visible = np.ones(keypoints.shape[:2], dtype=np.float32)

        if lifting_target is None:
            lifting_target = [keypoints[..., 0, :, :]]

        # set initial value for `lifting_target_weights`
        # and `trajectory_weights`
        if lifting_target_visible is None:
            lifting_target_visible = np.ones(
                lifting_target.shape[:-1], dtype=np.float32)
            lifting_target_weights = lifting_target_visible
            trajectory_weights = (1 / lifting_target[:, 2])
        else:
            valid = lifting_target_visible > 0.5
            lifting_target_weights = np.where(valid, 1., 0.).astype(np.float32)
            trajectory_weights = lifting_target_weights

        if camera_param is None:
            camera_param = dict()

        encoded = dict()

        lifting_target_label = lifting_target.copy()
        keypoint_labels = keypoints.copy()

        assert keypoint_labels.ndim in {2, 3}
        if keypoint_labels.ndim == 2:
            keypoint_labels = keypoint_labels[None, ...]

        # Normalize the 2D keypoint coordinate with image width and height
        _camera_param = deepcopy(camera_param)
        assert 'w' in _camera_param and 'h' in _camera_param
        w, h = _camera_param['w'], _camera_param['h']
        keypoint_labels[
            ..., :2] = keypoint_labels[..., :2] / w * 2 - [1, h / w]

        # convert target to image coordinate
        T = keypoint_labels.shape[0]
        factor_ = np.array([4] * T, dtype=np.float32).reshape(T, )
        if 'f' in _camera_param and 'c' in _camera_param:
            lifting_target_label, factor_ = camera_to_image_coord(
                self.root_index, lifting_target_label, _camera_param)
        lifting_target_label[..., :, :] = lifting_target_label[
            ..., :, :] - lifting_target_label[...,
                                              self.root_index:self.root_index +
                                              1, :]
        if factor is None or factor[0] == 0:
            factor = factor_
        if factor.ndim == 1:
            factor = factor[:, None]
        if self.factor_label:
            lifting_target_label *= factor[..., None]

        if self.concat_vis:
            keypoints_visible_ = keypoints_visible
            if keypoints_visible.ndim == 2:
                keypoints_visible_ = keypoints_visible[..., None]
            keypoint_labels = np.concatenate(
                (keypoint_labels, keypoints_visible_), axis=2)

        encoded['keypoint_labels'] = keypoint_labels
        encoded['keypoint_labels_visible'] = keypoints_visible
        encoded['lifting_target_label'] = lifting_target_label
        encoded['lifting_target_weights'] = lifting_target_weights
        encoded['lifting_target'] = lifting_target_label
        encoded['lifting_target_visible'] = lifting_target_visible
        encoded['trajectory_weights'] = trajectory_weights
        encoded['factor'] = factor

        return encoded

    def decode(
        self,
        encoded: np.ndarray,
        w: Optional[np.ndarray] = None,
        h: Optional[np.ndarray] = None,
        factor: Optional[np.ndarray] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Decode keypoint coordinates from normalized space to input image
        space.

        Args:
            encoded (np.ndarray): Coordinates in shape (N, K, C).
            w (np.ndarray, optional): The image widths in shape (N, ).
                Default: ``None``.
            h (np.ndarray, optional): The image heights in shape (N, ).
                Default: ``None``.
            factor (np.ndarray, optional): The factor for projection in shape
                (N, ). Default: ``None``.

        Returns:
            keypoints (np.ndarray): Decoded coordinates in shape (N, K, C).
            scores (np.ndarray): The keypoint scores in shape (N, K).
        """
        keypoints = encoded.copy()
        scores = np.ones(keypoints.shape[:-1], dtype=np.float32)

        if self.rootrel:
            keypoints[..., 0, :] = 0

        if w is not None and w.size > 0:
            assert w.shape == h.shape
            assert w.shape[0] == keypoints.shape[0]
            assert w.ndim in {1, 2}
            if w.ndim == 1:
                w = w[:, None]
                h = h[:, None]
            trans = np.append(
                np.ones((w.shape[0], 1)), h / w, axis=1)[:, None, :]
            keypoints[..., :2] = (keypoints[..., :2] + trans) * w[:, None] / 2
            keypoints[..., 2:] = keypoints[..., 2:] * w[:, None] / 2
        if factor is not None and factor.size > 0:
            assert factor.shape[0] == keypoints.shape[0]
            keypoints *= factor[..., None]
        keypoints[..., :, :] = keypoints[..., :, :] - keypoints[
            ..., self.root_index:self.root_index + 1, :]
        keypoints /= 1000.
        return keypoints, scores