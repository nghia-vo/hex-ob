#!/usr/bin/env python3
"""
Find the roll and tilt of the rotation axis from an alignment scan.

ONE shared implementation of the legacy per-detector scripts (they were
~99% identical):
    hex-acq-pyepics/techniques/tomography/kinetix/check_alignment.py
    hex-acq-pyepics/techniques/tomography/dual_cam/check_alignment.py
    hex-acq-pyepics/techniques/tomography/phantom/check_alignment.py

This is OFFLINE ANALYSIS (algotom/skimage/matplotlib), not a Bluesky plan.
The sphere-extraction and ellipse/linear fitting math is copied verbatim
from the originals; only the data loading changed for the hex-ob plans'
output layout: ONE HDF file per scan, flats appended after the projections
(alignment_scan) or in a separate dark/flat file (take_dark_flat, darks
first then flats).

Usage
-----
    # alignment_scan output: last N frames of the same file are flats
    ./check_alignment.py /path/to/scan_00001/proj.h5 --num-flats 2

    # flats from a take_dark_flat file (darks first, flats last)
    ./check_alignment.py proj.h5 --flat dark_flat.h5 --flat-frames 50

    # no flats anywhere: median of the projections is used (as the
    # original did)
    ./check_alignment.py proj.h5

    # inspect what would be loaded without running the analysis
    ./check_alignment.py proj.h5 --num-flats 2 --info

Requires numpy/h5py for loading; scipy/skimage/algotom/matplotlib for the
analysis itself (imported lazily so --info works without them).
"""

import argparse
import glob
import os
import sys

import h5py
import numpy as np

HDF_KEY = "entry/data/data"


def resolve_hdf(path: str) -> str:
    """Accept an HDF file path or a scan directory containing one."""
    if os.path.isfile(path):
        return path
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*proj*.h*"))) or sorted(
            glob.glob(os.path.join(path, "*.h5"))
            + glob.glob(os.path.join(path, "*.hdf*"))
        )
        if files:
            return files[0]
    raise ValueError(f"No HDF file found at: {path}")


def load_frames(path: str) -> np.ndarray:
    with h5py.File(resolve_hdf(path), "r") as f:
        return np.asarray(f[HDF_KEY][:])


def load_inputs(args):
    """Return (proj_data, flat) per the CLI flags."""
    data = load_frames(args.proj)
    if args.num_flats and args.num_flats > 0:
        if args.num_flats >= len(data):
            raise ValueError(
                f"--num-flats {args.num_flats} >= total frames {len(data)}"
            )
        proj_data = data[: -args.num_flats]
        flat = np.mean(np.float32(data[-args.num_flats:]), axis=0)
    elif args.flat:
        proj_data = data
        flat_frames = load_frames(args.flat)
        if args.flat_frames and args.flat_frames > 0:
            flat_frames = flat_frames[-args.flat_frames:]  # flats follow darks
        flat = np.mean(np.float32(flat_frames), axis=0)
    else:
        proj_data = data
        flat = np.squeeze(np.median(proj_data, axis=0))
    flat = np.float32(flat)
    flat[flat == 0.0] = np.mean(flat)
    return proj_data, flat


# ---------------------------------------------------------------------------
# Analysis — verbatim from the legacy scripts
# ---------------------------------------------------------------------------

def clean_image(binary_image, size_threshold=100):
    """Clean binary image."""
    import scipy.ndimage as ndi
    from skimage import measure, segmentation

    binary_image = segmentation.clear_border(binary_image)
    binary_image = ndi.binary_opening(binary_image, iterations=2)
    binary_image = ndi.binary_fill_holes(binary_image)

    label_image = measure.label(binary_image)
    properties = measure.regionprops(label_image)

    size_mask = np.zeros_like(binary_image, dtype=bool)
    for prop in properties:
        if prop.area >= size_threshold:
            size_mask[label_image == prop.label] = True
    return np.logical_and(binary_image, size_mask)


def fit_points_to_ellipse(x, y):
    if len(x) != len(y):
        raise ValueError("x and y must have the same length!!!")
    A = np.array([x ** 2, x * y, y ** 2, x, y, np.ones_like(x)]).T
    vh = np.linalg.svd(A, full_matrices=False)[-1]
    a0, b0, c0, d0, e0, f0 = vh.T[:, -1]
    denom = b0 ** 2 - 4 * a0 * c0
    msg = "Can't fit to an ellipse!!!"
    if denom == 0:
        raise ValueError(msg)
    xc = (2 * c0 * d0 - b0 * e0) / denom
    yc = (2 * a0 * e0 - b0 * d0) / denom
    roll_angle = np.rad2deg(
        np.arctan2(c0 - a0 - np.sqrt((a0 - c0) ** 2 + b0 ** 2), b0))
    if roll_angle > 90.0:
        roll_angle = - (180 - roll_angle)
    if roll_angle < -90.0:
        roll_angle = (180 + roll_angle)
    a_term = 2 * (a0 * e0 ** 2 + c0 * d0 ** 2 - b0 * d0 * e0 + denom * f0) * (
            a0 + c0 + np.sqrt((a0 - c0) ** 2 + b0 ** 2))
    if a_term < 0.0:
        raise ValueError(msg)
    a_major = -2 * np.sqrt(a_term) / denom
    b_term = 2 * (a0 * e0 ** 2 + c0 * d0 ** 2 - b0 * d0 * e0 + denom * f0) * (
            a0 + c0 - np.sqrt((a0 - c0) ** 2 + b0 ** 2))
    if b_term < 0.0:
        raise ValueError(msg)
    b_minor = -2 * np.sqrt(b_term) / denom
    if a_major < b_minor:
        a_major, b_minor = b_minor, a_major
        if roll_angle < 0.0:
            roll_angle = 90 + roll_angle
        else:
            roll_angle = -90 + roll_angle
    return roll_angle, a_major, b_minor, xc, yc


def identify_sign_tilt_angle(x, y):
    """
    Find the two points at the furthest distance and their indices,
    perform linear fit using these points.
    """
    data_points = np.asarray(list(zip(x, y)))
    max_dist = 0
    index1, index2 = 0, 0
    for i in range(len(data_points)):
        for j in range(i + 1, len(data_points)):
            dist = np.linalg.norm(data_points[i] - data_points[j])
            if dist > max_dist:
                max_dist = dist
                index1, index2 = i, j
    x_furthest = [data_points[index1][0], data_points[index2][0]]
    y_furthest = [data_points[index1][1], data_points[index2][1]]
    slope, intercept = np.polyfit(x_furthest, y_furthest, 1)

    min_index, max_index = min(index1, index2), max(index1, index2)
    y_dis = []
    for i in range(min_index, max_index + 1):
        x_i = data_points[i, 0]
        y_i = data_points[i, 1]
        y_fit = slope * x_i + intercept
        y_dis.append(y_i - y_fit)
    y_median = np.median(np.asarray(y_dis))
    return 1 if y_median < 0 else -1


def run_analysis(proj_data, flat, args):
    import matplotlib.pyplot as plt
    import scipy.ndimage as ndi
    import algotom.util.calibration as calib

    figsize = (14, 7)
    (depth, height, width) = proj_data.shape
    if depth < 36:
        raise ValueError(
            f"\n\nPlease check the number of projections: {depth}. "
            "It must be > 36\n\n"
        )
    left = args.crop_left
    right = width - args.crop_right
    top = args.crop_top
    bottom = height - args.crop_bottom
    width_cr = right - left
    if width_cr <= 1:
        raise ValueError("!!!Please check options for cropping left and right"
                         "\nNote that the image is cropped from the border!!!")
    height_cr = bottom - top
    if height_cr <= 1:
        raise ValueError("!!!Please check options for cropping top and bottom"
                         "\nNote that the image is cropped from the border!!!")
    list_angle = np.linspace(0, 360, depth)

    x_centers, y_centers, img_list = [], [], []
    print("\n=============================================")
    print("Extract the sphere and get its center-of-mass\n")

    for i, img in enumerate(proj_data):
        mat = img[top:bottom, left:right] / flat[top:bottom, left:right]
        mat = ndi.gaussian_filter(mat, 5)
        threshold = calib.calculate_threshold(mat, bgr='bright')
        mat_bin0 = calib.binarize_image(mat, threshold=args.ratio * threshold,
                                        bgr='bright')
        mat_bin0 = clean_image(mat_bin0)
        nmean = np.sum(mat_bin0)
        if nmean < 20.0:
            print("\n******************************************************")
            print("Adjust ratio of threshold or the field of view to get the sphere!")
            print(f"Current used ratio: {args.ratio} and threshold: {threshold} ")
            print("********************************************************")
            plt.figure(figsize=figsize)
            plt.imshow(mat, cmap="gray")
            plt.show()
            raise ValueError("No binary sphere detected! Please adjust parameters!")
        sphere_size = calib.get_dot_size(mat_bin0, size_opt="max")
        mat_bin = calib.select_dot_based_size(mat_bin0, sphere_size)
        (y_cen, x_cen) = ndi.center_of_mass(mat_bin)
        x_centers.append(x_cen)
        y_centers.append(height_cr - y_cen)
        img_list.append(mat)
        print(f"  ---> Done image: {i:2} | Angle: {list_angle[i]:3.1f} | "
              f"Center X: {x_cen:4.2f} | Center Y: {y_cen:4.2f}")

    x = np.float32(x_centers)
    y = np.float32(y_centers)
    img_overlay = np.mean(np.asarray(img_list), axis=0)

    fit_ellipse = args.method == "ellipse"
    if fit_ellipse:
        (a, b) = np.polyfit(x, y, 1)[:2]
        dist_list = np.abs(a * x - y + b) / np.sqrt(a ** 2 + 1)
        dist_list = ndi.gaussian_filter1d(dist_list, 2)
        if np.max(dist_list) < 1.0:
            fit_ellipse = False
            print("\nDistances of points to a fitted line is small, "
                  "Use a linear-fit method instead!\n")

    if fit_ellipse:
        try:
            result = fit_points_to_ellipse(x, y)
            roll_angle, major_axis, minor_axis, xc, yc = result
            tilt_angle = np.rad2deg(np.arctan2(minor_axis, major_axis))
        except ValueError:
            fit_ellipse = False
            print("\nCan't fit points to an ellipse, using a linear-fit "
                  "method instead!\n")

    if not fit_ellipse:
        (a, b) = np.polyfit(x, y, 1)[:2]
        dist_list = np.abs(a * x - y + b) / np.sqrt(a ** 2 + 1)
        appr_major = np.max(np.asarray(
            [np.sqrt((x[i] - x[j]) ** 2 + (y[i] - y[j]) ** 2) for i in
             range(len(x)) for j in range(i + 1, len(x))]))
        dist_list = ndi.gaussian_filter1d(dist_list, 2)
        appr_minor = 2.0 * np.max(dist_list)
        tilt_angle = np.rad2deg(np.arctan2(appr_minor, appr_major))
        roll_angle = np.rad2deg(np.arctan(a))

    tilt_angle = abs(tilt_angle) * identify_sign_tilt_angle(x, y)

    print("=============================================")
    print("Roll angle: {} degree".format(roll_angle))
    print("Tilt angle: {} degree".format(tilt_angle))
    print("=============================================\n")

    plt.figure(1, figsize=figsize)
    plt.imshow(img_overlay, cmap="gray", extent=(0, width_cr, 0, height_cr))
    plt.tight_layout(rect=[0, 0, 1, 1])

    plt.figure(0, figsize=figsize)
    for i in range(len(x)):
        plt.plot(x[i], y[i], 'o', markersize=10, color="cyan")
        plt.text(x[i], y[i], str(i), fontsize=10, fontweight="bold",
                 ha='center', va='center', color='red')
    plt.title("Roll : {0:2.4f}; Tilt : {1:2.4f} (degree)".format(
        roll_angle, tilt_angle))
    if fit_ellipse:
        angle = np.radians(roll_angle)
        theta = np.linspace(0, 2 * np.pi, 100)
        x_fit = (xc + 0.5 * major_axis * np.cos(theta) * np.cos(angle)
                 - 0.5 * minor_axis * np.sin(theta) * np.sin(angle))
        y_fit = (yc + 0.5 * major_axis * np.cos(theta) * np.sin(angle)
                 + 0.5 * minor_axis * np.sin(theta) * np.cos(angle))
        plt.plot(x_fit, y_fit, color="red")
    else:
        plt.plot(x, a * x + b, color="red")
    plt.xlabel("x")
    plt.ylabel("y")
    plt.tight_layout()
    plt.show()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("proj", help="Projections HDF file (or scan directory)")
    parser.add_argument("--num-flats", type=int, default=0,
                        help="Last N frames of the proj file are flats "
                             "(alignment_scan layout)")
    parser.add_argument("--flat", default=None,
                        help="Separate flat HDF file/directory "
                             "(e.g. take_dark_flat output)")
    parser.add_argument("--flat-frames", type=int, default=0,
                        help="Use the last N frames of --flat (flats follow "
                             "darks in take_dark_flat files)")
    parser.add_argument("-l", dest="crop_left", type=int, default=0)
    parser.add_argument("-r", dest="crop_right", type=int, default=0)
    parser.add_argument("-t", dest="crop_top", type=int, default=500)
    parser.add_argument("-b", dest="crop_bottom", type=int, default=500)
    parser.add_argument("-m", dest="method", default="ellipse",
                        choices=("ellipse", "linear"))
    parser.add_argument("--ratio", type=float, default=1.0,
                        help="Threshold adjustment for binarization")
    parser.add_argument("--info", action="store_true",
                        help="Print what would be loaded, then exit "
                             "(no analysis dependencies needed)")
    args = parser.parse_args(argv)

    proj_data, flat = load_inputs(args)
    print(f"Projections: {proj_data.shape} from {resolve_hdf(args.proj)}")
    print(f"Flat: {flat.shape}"
          + (" (median of projections)" if not (args.num_flats or args.flat) else ""))
    if args.info:
        return 0

    run_analysis(proj_data, flat, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
