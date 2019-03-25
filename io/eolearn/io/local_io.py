"""
Module containing tasks used for reading and writing to disk
"""

import os.path

import datetime
import rasterio
import numpy as np
from pyproj import Proj, transform
from sentinelhub import CRS
from sentinelhub.time_utils import iso_to_datetime

from eolearn.core import EOTask, SaveToDisk, FeatureType


class ExportToTiff(SaveToDisk):
    """ Task exports specified feature to Geo-Tiff.

    When exporting multiple times OR bands, the Geo-Tiff `band` counts are in the expected order.
    However, when exporting multiple times AND bands, the order obeys the following pattern:

    T(1)B(1), T(1)B(2), ..., T(1)B(N), T(2)B(1), T(2)B(2), ..., T(2)B(N), ..., ..., T(M)B(N)

    where T and B are the time and band indices of the array,
    and M and N are the lengths of these indices, respectively/

    :param feature: Feature which will be exported
    :type feature: (FeatureType, str)
    :param folder: root directory where all Geo-Tiff images will be saved
    :type folder: str
    :param band_indices: Bands to be added to tiff image. Bands are represented by their 0-based index as
    tuple in the form `(start_band, end_band)` or as list in the form `[band_1, band_2,...,band_n]`.
    :type band_indices: tuple or list
    :param date_indices: Dates to be added to tiff image. Dates are represented by their 0-based index as
    tuple in the form `(start_date, end_date)` or a list in the form `[date_1, date_2,...,date_n]`.
    :type date_indices: tuple or list
    :param image_dtype: Type of data to be saved into tiff image
    :type image_dtype: numpy.dtype
    :param no_data_value: Value of pixels of tiff image with no data in EOPatch
    :type no_data_value: int or float
    """

    def __init__(self, feature, folder='.', *, band_indices=[0], date_indices=[0],
                 image_dtype=np.uint8, no_data_value=0):
        super().__init__(folder)

        self.feature = self._parse_features(feature)
        self.band_indices = band_indices
        self.date_indices = date_indices
        self.image_dtype = image_dtype
        self.no_data_value = no_data_value

    def _get_bands_subset(self, array):

        bands = np.array(range(array.shape[-1]))

        if isinstance(self.band_indices, list):
            if [band for band in self.band_indices if not isinstance(band, int)]:
                raise ValueError('Invalid format in {} list, expected integers'.format(self.band_indices))
            array_sub = array[..., np.array(self.band_indices)]
        elif isinstance(self.band_indices, tuple):
            if tuple(map(type, self.band_indices)) != (int, int):
                raise ValueError('Invalid format in {} tuple, expected integers'.format(self.band_indices))
            array_sub = array[..., np.nonzero(np.where(
                (bands >= self.band_indices[0]) & (bands <= self.band_indices[1]), bands, 0))[0]]
        else:
            raise ValueError('Invalid format in {}, expected tuple or list'.format(self.band_indices))

        return array_sub

    def _get_dates_subset(self, array, dates):

        dates = np.array(dates)

        if isinstance(self.date_indices, list):
            if [date for date in self.date_indices if not isinstance(date, int)]:
                raise ValueError('Invalid format in {} list, expected integers'.format(self.date_indices))
            array_sub = array[np.array(self.date_indices)]
        elif isinstance(self.date_indices, tuple):
            if tuple(map(type, self.date_indices)) == (int, int):
                start_date = dates[self.date_indices[0]]
                end_date = dates[self.date_indices[1]]
            elif tuple(map(type, self.date_indices)) == (str, str):
                start_date = iso_to_datetime(self.date_indices[0])
                end_date = iso_to_datetime(self.date_indices[1])
            elif tuple(map(type, self.date_indices)) == (datetime.datetime, datetime.datetime):
                start_date = self.date_indices[0]
                end_date = self.date_indices[1]
            else:
                raise ValueError('Invalid format in {} tuple, expected ints, strings, or datetimes'.format(
                    self.date_indices))
            array_sub = array[np.nonzero(np.where((dates >= start_date) & (dates <= end_date), dates, 0))[0]]
        else:
            raise ValueError('Invalid format in {}, expected tuple or list'.format(self.date_indices))

        return array_sub

    def execute(self, eopatch, *, filename):

        feature_type, feature_name = next(self.feature(eopatch))
        array = eopatch[feature_type][feature_name]

        array_sub = self._get_bands_subset(array)

        if feature_type in [FeatureType.DATA, FeatureType.MASK, FeatureType.SCALAR]:
            array_sub = self._get_dates_subset(array_sub, eopatch.timestamp)

            if feature_type is FeatureType.SCALAR:
                # add height and width dimensions
                array_sub = np.expand_dims(np.expand_dims(array_sub, axis=1), axis=1)

        else:
            # add temporal dimension
            array_sub = np.expand_dims(array_sub, axis=0)
            if feature_type is FeatureType.SCALAR_TIMELESS:
                # add height and width dimensions
                array_sub = np.expand_dims(np.expand_dims(array_sub, axis=1), axis=1)

        time_dim, height, width, band_dim = array_sub.shape

        index = time_dim * band_dim
        dst_transform = rasterio.transform.from_bounds(*eopatch.bbox, width=width, height=height)
        dst_crs = {'init': CRS.ogc_string(eopatch.bbox.crs)}

        # Write it out to a file.
        with rasterio.open(os.path.join(self.folder, filename), 'w', driver='GTiff',
                           width=width, height=height,
                           count=index,
                           dtype=self.image_dtype, nodata=self.no_data_value,
                           transform=dst_transform, crs=dst_crs) as dst:
            output_array = array_sub.astype(self.image_dtype)
            output_array = np.moveaxis(output_array, -1, 1).reshape(index, height, width)
            dst.write(output_array)

        return eopatch

class ReadFromTiff(EOTask):
    """ Read from a local or remote GeoTiff into a EOPatch.

    :param path: Path of a GeoTIFF to add to the EOPatch
    :type path: str
    :param mask_name: name of the MASK_TIMELESS feature that the GeoTiff data will be added to
    :type mask_name: str
     """

    def __init__(self, path, mask_name):
        self.path = path
        self.mask_name = mask_name

    def _get_window(self, src, bbox):
        x_res, y_res = src.transform[0], src.transform[4]
        p1 = Proj(bbox.get_crs().ogc_string())
        p2 = Proj(**src.crs)

        # project mask boundaries from bbox CRS to source CRS
        tile_ul_proj = transform(p1, p2, bbox.min_x, bbox.max_y)
        tile_lr_proj = transform(p1, p2, bbox.max_x, bbox.min_y)
        # get origin point from the TIF
        tif_ul_proj = (src.bounds.left, src.bounds.top)

        # use the above information to calculate the pixel indices of the window
        top = int((tile_ul_proj[1] - tif_ul_proj[1]) / y_res)
        left = int((tile_ul_proj[0] - tif_ul_proj[0]) / x_res)
        bottom = int((tile_lr_proj[1] - tif_ul_proj[1]) / y_res)
        right = int((tile_lr_proj[0] - tif_ul_proj[0]) / x_res)

        return ((top, bottom), (left, right))

    def _get_width_height(self, src, bbox):
        window = self._get_window(src, bbox)
        ((top, bottom), (left, right)) = window
        return (window[1][1] - window[1][0], window[0][1] - window[0][0])

    def execute(self, eopatch, width=None, height=None):
        """ Execute function which adds new MASK_TIMELESS layer to the EOPatch

        :param eopatch: input EOPatch
        :type eopatch: EOPatch
        :param width: optional parameter specifying the width of the new mask
        :type width: int
        :param height: optional parameter specifying the height of the new mask
        :type height: int
        :return: New EOPatch with added vector layer
        :rtype: EOPatch
        """
        bbox = eopatch.bbox

        with rasterio.open(self.path) as src:
            if not width or not height:
                width, height = self._get_width_height(src, bbox)
            bands = src.count
            data = np.empty(shape=(bands, width, height)).astype(src.profile['dtype'])
            window = self._get_window(src, bbox)
            for band in range(bands):
                src.read(band + 1, window=window, out=data[band], boundless=True)

        eopatch[FeatureType.MASK_TIMELESS][self.mask_name] = np.moveaxis(data, 0, -1)
        return eopatch
