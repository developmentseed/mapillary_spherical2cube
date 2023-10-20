import requests
from joblib import Parallel, delayed
from tqdm import tqdm
import os
from pathlib import Path
import glob
import cv2
from PIL import Image
from smart_open import open
import lensfunpy
from copy import deepcopy
from itertools import chain

access_token = os.environ.get("MAPILLARY_ACCESS_TOKEN")


def cubemap_splitter(
        img_file_cubemap,
        image_clip_size,
        sequence_id,
        image_id,
        output_images_path,
        cube_sides,
):
    """Split cubemap images

    Args:
        img_file_cubemap (str): Location of cubemap image
        image_clip_size (int): Size of the image to clip
        sequence_id (str): Mapillary sequece id
        image_id (ssstr): Mapillary img id
        output_images_path (str): Location to save the images
        cube_sides (str): Sides to processes the image
    """
    try:
        img = cv2.imread(img_file_cubemap)
        img_height = img.shape[0]
        img_width = img.shape[1]
        r = img_width - img_height
        h = w = image_clip_size
        horizontal_chunks = 4
        vertical_chunks = 3
        index_dict = {
            "1,0": "top",
            "0,1": "left",
            "1,1": "front",
            "2,1": "right",
            "3,1": "back",
            "1,2": "bottom",
        }
        sides = cube_sides.split(",")
        status_response = []
        for x in range(0, horizontal_chunks):
            for y in range(0, vertical_chunks):
                index = f"{x},{y}"
                if index in index_dict.keys() and index_dict[index] in sides:
                    try:
                        crop_img = img[y * r: y * r + h, x * r: x * r + w]
                        imageRGB = cv2.cvtColor(crop_img, cv2.COLOR_BGR2RGB)
                        img_ = Image.fromarray(imageRGB, mode="RGB")
                        chunk_img_path = f"{output_images_path}/{sequence_id}/{image_id}_{index_dict[index]}.jpg"
                        with open(chunk_img_path, "wb") as sfile:
                            img_.save(sfile)
                            status_response.append(chunk_img_path)
                    except Exception as ex:
                        print(ex)
                        status_response.append(False)
        return status_response
    except Exception as ex:
        print(f"error: {ex} in {output_images_path}/{sequence_id}/{image_id}.jpg")
        return [False] * len(cube_sides.split(","))


def download_clip_img(feature, output_images_path, image_clip_size, cube_sides):
    """Download and clip the spherical image

    Args:
        feature (dict): feture
        output_images_path (str): path to save the images
        image_clip_size (int): Size of the image to clip
        cube_sides (string): sides to process images
    Returns:
        list: features
    """
    sequence_id = feature["properties"]["sequence_id"]
    image_folder_path = f"{output_images_path}/{sequence_id}"
    if not os.path.exists(image_folder_path):
        os.makedirs(image_folder_path)

    # request the URL of each image
    image_id = feature["properties"]["id"]

    # Check if mapillary image exist and download
    img_file_equirectangular = f"{image_folder_path}/{image_id}.jpg"
    img_file_cubemap = f"{image_folder_path}/{image_id}_cubemap.jpg"

    header = {"Authorization": "OAuth {}".format(access_token)}
    url = "https://graph.mapillary.com/{}?fields=thumb_original_url".format(image_id)
    results = []

    if all([Path(f"{image_folder_path}/{image_id}_{i}.jpg").exists() for i in cube_sides.split(',')]):
        for i in cube_sides.split(','):
            new_feature = deepcopy(feature)
            new_feature["properties"]["image_path"] = f"{image_folder_path}/{image_id}_{i}.jpg"
            results.append(deepcopy(new_feature))
        return results

    try:
        r = requests.get(url, headers=header)
        data = r.json()
        image_url = data["thumb_original_url"]

        with open(img_file_equirectangular, "wb") as handler:
            image_data = requests.get(image_url, stream=True).content
            handler.write(image_data)

        # Convert Equirectangular -> Cubemap
        cmd = f"convert360 --convert e2c --i {img_file_equirectangular}  --o {img_file_cubemap} --w {image_clip_size}"
        os.system(cmd)

        # Split Cubemap to simple images
        result_status = cubemap_splitter(
            img_file_cubemap,
            image_clip_size,
            sequence_id,
            image_id,
            output_images_path,
            cube_sides,
        )
        # Rename files
        clean_files(image_folder_path, image_id, cube_sides)
        for i in result_status:
            if i:
                new_feature = deepcopy(feature)
                new_feature["properties"]["image_path"] = i
                results.append(deepcopy(new_feature))
    except Exception as err:
        print(err)

    return results


def clean_files(image_folder_path, image_id, cube_sides):
    """Remove files that was uploaded to s3, in order to optimize the fargate disk

    Args:
        image_folder_path (str): Location of the folder
        image_id (str): Id of the image
        cube_sides (str): sides to process images
    """
    chumk_image_path = f"{image_folder_path}/{image_id}"
    images_generate = [f"{chumk_image_path}_{i}.jpg" for i in cube_sides.split(",")]
    images_remove = [i for i in glob.glob(f"{chumk_image_path}*.jpg") if i not in images_generate]
    for file in images_remove:
        try:
            os.remove(file)
        except Exception as e:
            print(f"An error occurred: {str(e)}")


def process_image(features, output_images_path, image_clip_size, cube_sides):
    """Function to run in parallel mode to process mapillary images

    Args:
        features (fc): List of features objects
        output_images_path (str): Location to save clipped images
        image_clip_size (int): Size of the clipped image
        cube_sides (str): Sides of the image to clip

    Returns:
        fc: List of points that images were processed
    """
    # Process in parallel
    results2d = Parallel(n_jobs=-1)(
        delayed(download_clip_img)(
            feature, output_images_path, image_clip_size, cube_sides
        )
        for feature in tqdm(
            features, desc=f"Processing images for...", total=len(features)
        )
    )
    results1d = list(chain.from_iterable(list(results2d)))
    results = [i for i in results1d if i]

    return results


def correct_image(image_path, cam, len, focal_length, aperture, distance):
    """Correct fisheye image

    Args:
        image_path (str): localtion of the image
        cam (camera model): camera model
        len (lents model): lens model
        focal_length (float): focal_length
        aperture (float): aperture
        distance (int): distance

    Returns:
        cv2 image array
    """
    im = cv2.imread(image_path)
    height, width = im.shape[0], im.shape[1]
    mod = lensfunpy.Modifier(len, cam.crop_factor, width, height)
    mod.initialize(focal_length, aperture, distance)
    undist_coords = mod.apply_geometry_distortion()
    im_undistorted = cv2.remap(im, undist_coords, None, cv2.INTER_LANCZOS4)
    return im_undistorted
