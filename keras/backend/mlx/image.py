import functools
import itertools
import operator

import mlx.core as mx

from keras.backend.mlx.core import convert_to_tensor
from keras.backend.mlx.core import to_mlx_dtype


def _mirror_index_fixer(index, size):
    s = size - 1  # Half-wavelength of triangular wave
    # Scaled, integer-valued version of the triangular wave |x - round(x)|
    return mx.abs((index + s) % (2 * s) - s)


def _reflect_index_fixer(index, size):
    return mx.floor_divide(
        _mirror_index_fixer(2 * index + 1, 2 * size + 1) - 1, 2
    )


_INDEX_FIXERS = {
    # we need to take care of out-of-bound indices in torch
    "constant": lambda index, size: mx.clip(index, 0, size - 1),
    "nearest": lambda index, size: mx.clip(index, 0, size - 1),
    "wrap": lambda index, size: index % size,
    "mirror": _mirror_index_fixer,
    "reflect": _reflect_index_fixer,
}


def _is_integer(a):
    # Should we add bool?
    return to_mlx_dtype(a.dtype) in (
        mx.int32,
        mx.uint32,
        mx.int64,
        mx.uint64,
        mx.int16,
        mx.uint16,
        mx.int8,
        mx.uint8,
    )


def _nearest_indices_and_weights(coordinate):
    coordinate = coordinate if _is_integer(coordinate) else mx.round(coordinate)
    index = coordinate.astype(mx.int32)
    return [(index, 1)]


def _linear_indices_and_weights(coordinate):
    lower = mx.floor(coordinate)
    upper_weight = coordinate - lower
    lower_weight = 1 - upper_weight
    index = lower.astype(mx.int32)
    return [(index, lower_weight), (index + 1, upper_weight)]


def _extract_coordinates(
    src,
    coordinates,
    interpolation_function,
    index_fixer,
    fill_value=0,
    check_validity=True,
    start_axis=0,
):
    def _expand(x):
        if not isinstance(x, mx.array):
            return x
        return x.reshape(*x.shape, *([1] * (src.ndim - x.ndim)))

    indices = []
    for ci, size in zip(coordinates, src.shape[start_axis:]):
        indices.append(
            [
                (
                    index_fixer(index, size),
                    _expand(mx.logical_and((0 <= index), (index < size))),
                    _expand(weight),
                )
                for index, weight in interpolation_function(ci)
            ]
        )

    outputs = []
    empty_slices = (slice(None),) * start_axis
    for items in itertools.product(*indices):
        indices, validities, weights = zip(*items)
        index = empty_slices + indices
        contribution = src[index]

        # Check if we need to replace some with fill value
        if check_validity:
            all_valid = functools.reduce(operator.and_, validities)
            contribution = mx.where(all_valid, contribution, fill_value)

        # Multiply with the weight if it isn't 1.0
        weight = functools.reduce(operator.mul, weights)
        if not (isinstance(weight, (float, int)) and weight == 1):
            contribution = contribution * weight

        outputs.append(contribution)

    result = functools.reduce(operator.add, outputs)
    if _is_integer(src) and not _is_integer(result):
        result = mx.round(result)

    return result.astype(src.dtype)


def map_coordinates(
    input, coordinates, order, fill_mode="constant", fill_value=0.0
):
    input_arr = convert_to_tensor(input)
    coordinate_arrs = [convert_to_tensor(c) for c in coordinates]
    # skip tensor creation as possible
    if isinstance(fill_value, (int, float)) and _is_integer(input_arr):
        fill_value = int(fill_value)

    if len(coordinates) != len(input_arr.shape):
        raise ValueError(
            "coordinates must be a sequence of length input.shape, but "
            f"{len(coordinates)} != {len(input_arr.shape)}"
        )

    index_fixer = _INDEX_FIXERS.get(fill_mode)
    if index_fixer is None:
        raise ValueError(
            "Invalid value for argument `fill_mode`. Expected one of "
            f"{set(_INDEX_FIXERS.keys())}. Received: fill_mode={fill_mode}"
        )

    if order == 0:
        interp_fun = _nearest_indices_and_weights
    elif order == 1:
        interp_fun = _linear_indices_and_weights
    else:
        raise NotImplementedError("map_coordinates currently requires order<=1")

    return _extract_coordinates(
        src=input_arr,
        coordinates=coordinate_arrs,
        interpolation_function=interp_fun,
        index_fixer=index_fixer,
        fill_value=fill_value,
        check_validity=fill_mode == "constant",
        start_axis=0,
    )


AFFINE_TRANSFORM_INTERPOLATIONS = {
    "nearest": _nearest_indices_and_weights,
    "bilinear": _linear_indices_and_weights,
}
AFFINE_TRANSFORM_FILL_MODES = {
    "constant",
    "nearest",
    "wrap",
    "mirror",
    "reflect",
}


def _affine_transform(
    src,
    transform,
    target_size,
    interpolation_function,
    index_fixer,
    fill_value=0,
    check_validity=True,
):
    y_target = mx.arange(target_size[0]).reshape(1, -1, 1)
    x_target = mx.arange(target_size[1]).reshape(1, 1, -1)
    a0, a1, a2, b0, b1, b2, c0, c1 = [
        t.reshape(-1, 1, 1)
        for t in transform.T.reshape(-1).reshape(8, -1).split(8)
    ]
    # TODO: Should we ignore c0 and c1 as the docs say they are only used in
    #       the tf backend?
    k = c0 * x_target + c1 * y_target + 1
    x_src = (a0 * x_target + a1 * y_target + a2) / k
    y_src = (b0 * x_target + b1 * y_target + b2) / k

    # not batched
    if src.ndim == 3:
        indices = [y_src.squeeze(0), x_src.squeeze(0)]

    # batched
    else:
        indices = [
            mx.arange(len(src)).reshape(-1, 1, 1),
            y_src,
            x_src,
        ]

    return _extract_coordinates(
        src=src,
        coordinates=indices,
        interpolation_function=interpolation_function,
        index_fixer=index_fixer,
        fill_value=fill_value,
        check_validity=check_validity,
        start_axis=0,
    )


def affine_transform(
    image,
    transform,
    interpolation="bilinear",
    fill_mode="constant",
    fill_value=0,
    data_format="channels_last",
):
    if interpolation not in AFFINE_TRANSFORM_INTERPOLATIONS.keys():
        raise ValueError(
            "Invalid value for argument `interpolation`. Expected of one "
            f"{set(AFFINE_TRANSFORM_INTERPOLATIONS.keys())}. Received: "
            f"interpolation={interpolation}"
        )
    if fill_mode not in AFFINE_TRANSFORM_FILL_MODES:
        raise ValueError(
            "Invalid value for argument `fill_mode`. Expected of one "
            f"{AFFINE_TRANSFORM_FILL_MODES}. Received: fill_mode={fill_mode}"
        )

    image = convert_to_tensor(image)
    transform = convert_to_tensor(transform)

    if image.ndim not in (3, 4):
        raise ValueError(
            "Invalid image rank: expected rank 3 (single image) "
            "or rank 4 (batch of images). Received input with shape: "
            f"image.shape={image.shape}"
        )
    if transform.ndim not in (1, 2):
        raise ValueError(
            "Invalid transform rank: expected rank 1 (single transform) "
            "or rank 2 (batch of transforms). Received input with shape: "
            f"transform.shape={transform.shape}"
        )

    if data_format == "channels_first":
        image = (
            image.transpose(0, 2, 3, 1)
            if image.ndim == 4
            else image.transpose(1, 2, 0)
        )

    result = _affine_transform(
        src=image,
        transform=transform,
        target_size=image.shape[:2] if image.ndim == 3 else image.shape[1:3],
        interpolation_function=AFFINE_TRANSFORM_INTERPOLATIONS[interpolation],
        index_fixer=_INDEX_FIXERS[fill_mode],
        fill_value=fill_value,
        check_validity=fill_mode == "constant",
    )

    if data_format == "channels_first":
        result = (
            result.transpose(0, 3, 1, 2)
            if image.ndim == 4
            else result.transpose(2, 0, 1)
        )

    return result


def resize(
    image,
    size,
    interpolation="bilinear",
    antialias=False,
    data_format="channels_last",
):
    if antialias:
        raise NotImplementedError(
            "Antialiasing not implemented for the MLX backend"
        )

    if interpolation not in AFFINE_TRANSFORM_INTERPOLATIONS.keys():
        raise ValueError(
            "Invalid value for argument `interpolation`. Expected of one "
            f"{set(AFFINE_TRANSFORM_INTERPOLATIONS.keys())}. Received: "
            f"interpolation={interpolation}"
        )

    size = tuple(size)
    image = convert_to_tensor(image)

    if image.ndim not in (3, 4):
        raise ValueError(
            "Invalid input rank: expected rank 3 (single image) "
            "or rank 4 (batch of images). Received input with shape: "
            f"image.shape={image.shape}"
        )

    # Change to channels_last
    if data_format == "channels_first":
        image = (
            image.transpose(0, 2, 3, 1)
            if image.ndim == 4
            else image.transpose(1, 2, 0)
        )

    *_, H, W, C = image.shape
    transform = mx.array([H / size[0], 0, 0, 0, W / size[1], 0, 0, 0])
    result = _affine_transform(
        src=image,
        transform=transform,
        target_size=size,
        interpolation_function=AFFINE_TRANSFORM_INTERPOLATIONS[interpolation],
        index_fixer=_INDEX_FIXERS["constant"],
        fill_value=0,
        check_validity=False,
    )

    # Change back to channels_first
    if data_format == "channels_first":
        result = (
            result.transpose(0, 3, 1, 2)
            if image.ndim == 4
            else result.transpose(2, 0, 1)
        )

    return result
