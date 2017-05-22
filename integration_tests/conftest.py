# coding=utf-8
"""
Common methods for index integration tests.
"""
from __future__ import absolute_import

import itertools
import logging
import os
import shutil
from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import numpy as np
import rasterio
import yaml

import datacube.utils
from datacube.index.postgres import _dynamic

try:
    from yaml import CSafeLoader as SafeLoader
except ImportError:
    from yaml import SafeLoader

from datacube.drivers.manager import DriverManager
from datacube.api import API
from datacube.config import LocalConfig
from datacube.index._api import Index, _DEFAULT_METADATA_TYPES_PATH
from datacube.index.postgres import PostgresDb
from datacube.index.postgres.tables import _core

_SINGLE_RUN_CONFIG_TEMPLATE = """
[locations]
testdata: {test_tile_folder}
eotiles: {eotiles_tile_folder}
"""

GEOTIFF_SIZE = (9721, 8521)  # (250, 250)  # (9721, 8521)
'''(width, height) of geotiff to create.'''

INTEGRATION_DEFAULT_CONFIG_PATH = Path(__file__).parent.joinpath('agdcintegration.conf')

_EXAMPLE_LS5_NBAR_DATASET_FILE = Path(__file__).parent.joinpath('example-ls5-nbar.yaml')
_TIME_SLICES = 1
'''Number of time slices to create in the sample data.'''

PROJECT_ROOT = Path(__file__).parents[1]
CONFIG_SAMPLES = PROJECT_ROOT / 'docs' / 'config_samples'
DATASET_TYPES = CONFIG_SAMPLES / 'dataset_types'
LS5_SAMPLES = CONFIG_SAMPLES / 'storage_types' / 'ga_landsat_5'
LS5_NBAR_INGEST_CONFIG = CONFIG_SAMPLES / 'ingester' / 'ls5_nbar_albers.yaml'
LS5_NBAR_STORAGE_TYPE = LS5_SAMPLES / 'ls5_geographic.yaml'
LS5_NBAR_NAME = 'ls5_nbar'
LS5_NBAR_ALBERS_STORAGE_TYPE = LS5_SAMPLES / 'ls5_albers.yaml'
LS5_NBAR_ALBERS_NAME = 'ls5_nbar_albers'

# Resolution and chunking shrink factors
TEST_STORAGE_SHRINK_FACTORS = (100, 100)
TEST_STORAGE_SHRINK_FACTORS_S3 = (100, 12)
TEST_STORAGE_NUM_MEASUREMENTS = 2
GEOGRAPHIC_VARS = ('latitude', 'longitude')
PROJECTED_VARS = ('x', 'y')

EXAMPLE_LS5_DATASET_ID = UUID('bbf3e21c-82b0-11e5-9ba1-a0000100fe80')


class MockIndex(object):
    def __init__(self, db):
        self._db = db


@pytest.fixture
def integration_config_paths(tmpdir):
    test_tile_folder = str(tmpdir.mkdir('testdata'))
    test_tile_folder = Path(test_tile_folder).as_uri()
    eotiles_tile_folder = str(tmpdir.mkdir('eotiles'))
    eotiles_tile_folder = Path(eotiles_tile_folder).as_uri()
    run_config_file = tmpdir.mkdir('config').join('test-run.conf')
    run_config_file.write(
        _SINGLE_RUN_CONFIG_TEMPLATE.format(test_tile_folder=test_tile_folder, eotiles_tile_folder=eotiles_tile_folder)
    )
    return (
        str(INTEGRATION_DEFAULT_CONFIG_PATH),
        str(run_config_file),
        os.path.expanduser('~/.datacube_integration.conf')
    )


@pytest.fixture
def global_integration_cli_args(integration_config_paths):
    """
    The first arguments to pass to a cli command for integration test configuration.
    """
    # List of a config files in order.
    return list(itertools.chain(*(('--config_file', f) for f in integration_config_paths)))


@pytest.fixture
def local_config(integration_config_paths):
    return LocalConfig.find(integration_config_paths)


@pytest.fixture(params=["US/Pacific", "UTC", ])
def db(local_config, request):
    timezone = request.param

    db = PostgresDb.from_config(local_config, application_name='test-run', validate_connection=False)

    # Drop and recreate tables so our tests have a clean db.
    with db.connect() as connection:
        _core.drop_db(connection._connection)
    remove_dynamic_indexes()

    # Disable informational messages since we're doing this on every test run.
    with _increase_logging(_core._LOG) as _:
        _core.ensure_db(db._engine)

    c = db._engine.connect()
    c.execute('alter database %s set timezone = %r' % (local_config.db_database, str(timezone)))
    c.close()

    # We don't need informational create/drop messages for every config change.
    _dynamic._LOG.setLevel(logging.WARN)

    yield db
    db.close()


@contextmanager
def _increase_logging(log, level=logging.WARN):
    previous_level = log.getEffectiveLevel()
    log.setLevel(level)
    yield
    log.setLevel(previous_level)


def remove_dynamic_indexes():
    """
    Clear any dynamically created indexes from the schema.
    """
    # Our normal indexes start with "ix_", dynamic indexes with "dix_"
    for table in _core.METADATA.tables.values():
        table.indexes.intersection_update([i for i in table.indexes if not i.name.startswith('dix_')])


@pytest.fixture(params=['NetCDF CF', 's3-test'])
def driver(db, request):
    '''Initialise all drivers and set current default one.

    Each driver has an index for which the passed `db` replaces the
    original db.
    '''
    # A hack to only run specific tests for s3-test:
    # if request.param == 's3-test' and not 'test_full_ingestion' in str(request._parent_request):
    #    pytest.skip('Skipping s3 test on everything but full ingestion for now')
    yield DriverManager(default_driver_name=request.param,
                        index=MockIndex(db)).driver
    # While not necessary, we reset the driver manager completely at
    # the end
    DriverManager().__instance = None


@pytest.fixture
def index(driver):
    return driver.index


@pytest.fixture
def dict_api(index):
    """
    :type index: datacube.index._api.Index
    """
    return API(index=index)


@pytest.fixture
def ls5_telem_doc(ga_metadata_type):
    return {
        "name": "ls5_telem_test",
        "description": 'LS5 Test',
        "metadata": {
            "platform": {
                "code": "LANDSAT_5"
            },
            "product_type": "satellite_telemetry_data",
            "ga_level": "P00",
            "format": {
                "name": "RCC"
            }
        },
        "metadata_type": ga_metadata_type.name
    }


@pytest.fixture
def ls5_telem_type(index, ls5_telem_doc):
    return index.products.add_document(ls5_telem_doc)


@pytest.fixture
def example_ls5_dataset_path(tmpdir):
    # Based on LS5_TM_NBAR_P54_GANBAR01-002_090_084_19900302
    dataset_dir = tmpdir.mkdir('ls5_dataset')
    shutil.copy(str(_EXAMPLE_LS5_NBAR_DATASET_FILE), str(dataset_dir.join('agdc-metadata.yaml')))

    # Write geotiffs
    geotiff_name = "LS5_TM_NBAR_P54_GANBAR01-002_090_084_19900302_B{}0.tif"
    scene_dir = dataset_dir.mkdir('product').mkdir('scene01')
    scene_dir.join('report.txt').write('Example')
    for num in (1, 2, 3):
        path = scene_dir.join(geotiff_name.format(num))
        create_empty_geotiff(str(path))

    return Path(str(dataset_dir))


@pytest.fixture
def example_ls5_dataset_paths(tmpdir):
    '''Create sample raw observations (dataset + geotiff).

    `_TIME_SLICES` observations are created by writing versions of
    `_EXAMPLE_LS5_NBAR_DATASET_FILE` with distinct date and ID
    parameters and creating geotiffs accordingly. 3 bands are created.

    :param tmpdir: The temp directoru in which to create the datasets.
    :return: dict: Dict of directories containing each observation,
      indexed by dataset UUID.

    '''
    start = datetime(1990, 3, 2)
    scene_name = 'LS5_TM_NBAR_P54_GANBAR01-002_090_084_{0:%Y%m%d}'
    dataset_dir = tmpdir.mkdir('ls5_dataset')
    dataset_dirs = {}
    with open(str(_EXAMPLE_LS5_NBAR_DATASET_FILE), 'r') as yaml_file:
        yaml = yaml_file.read()

    # We make a single geotiff file and copy it to each time slice and
    # band, to save time
    geotiff_path = dataset_dir.join('generic.tif')
    geotiff = create_empty_geotiff(str(geotiff_path))

    for time_count in range(_TIME_SLICES):
        # Create one time slice each 24h after the start date
        day = start + timedelta(days=time_count)
        obs_name = scene_name.format(day)
        obs_dir = dataset_dir.mkdir(obs_name)
        uuid = uuid4()
        # Replace all items that must be unique: dates (2 formats),
        # Dataset UUID, UUIDs appearing in lineage. Remove `output`
        # subdirectory.
        day_yaml = yaml \
            .replace(start.strftime('%Y-%m-%d'), day.strftime('%Y-%m-%d')) \
            .replace(start.strftime('%Y%m%d'), day.strftime('%Y%m%d')) \
            .replace('bbf3e21c-82b0-11e5-9ba1-a0000100fe80', str(uuid)) \
            .replace('ee983642-1cd3-11e6-aaba-a0000100fe80', str(uuid4())) \
            .replace('100a8412-6017-11e5-b4fe-ac162d791418', str(uuid4())) \
            .replace('product/', '')
        with open(str(obs_dir.join('agdc-metadata.yaml')), 'w') as yaml_file:
            yaml_file.write(day_yaml)
        scene_dir = obs_dir.mkdir('scene01')
        scene_dir.join('report.txt').write('Example')
        geotiff_name = '%s_B{}0.tif' % obs_name
        for band in (1, 2, 3):
            path = scene_dir.join(geotiff_name.format(band))
            shutil.copy(str(geotiff_path), str(path))
        dataset_dirs[uuid] = Path(str(obs_dir))

    os.remove(str(geotiff_path))
    return dataset_dirs


@pytest.fixture
def ls5_nbar_ingest_config(tmpdir, driver):
    dataset_dir = tmpdir.mkdir('ls5_nbar_ingest_test')
    config = load_yaml_file(LS5_NBAR_INGEST_CONFIG)[0]
    config = alter_dataset_type_for_testing(config, driver=driver.name)
    # config['storage']['chunking']['time'] = 2
    # config['storage']['tile_size']['time'] = 3
    config['location'] = str(dataset_dir)
    if 'storage' in config and \
       'driver' in config['storage'] and \
       config['storage']['driver'] in ('s3', 's3-test'):
        config['container'] = str(dataset_dir)

    config_path = dataset_dir.join('ls5_nbar_ingest_config.yaml')
    with open(str(config_path), 'w') as stream:
        yaml.dump(config, stream)
    return config_path, config


def create_empty_geotiff(path):
    metadata = {'count': 1,
                'crs': 'EPSG:28355',
                'driver': 'GTiff',
                'dtype': 'int16',
                'height': GEOTIFF_SIZE[1],
                'nodata': -999.0,
                'transform': [25.0, 0.0, 638000.0, 0.0, -25.0, 6276000.0],
                'width': GEOTIFF_SIZE[0]}
    with rasterio.open(path, 'w', **metadata) as dst:
        pass
        # Write in corners (fast)
        # data = np.zeros(GEOTIFF_SIZE, dtype=np.int16)
        # data[0][0] = 100
        # data[GEOTIFF_SIZE[0] - 1][0] = 200
        # data[0][GEOTIFF_SIZE[1] - 1] = 300
        # data[GEOTIFF_SIZE[0] - 1][GEOTIFF_SIZE[1] - 1] = 400
        # print('>>>>>>>>>>', data)

        # Write arranged data (slow)
        # dst.write(data, 1) #.astype(rasterio.int16), 1)
        # for i in range(GEOTIFF_SIZE[0]):
        #     for j in range(GEOTIFF_SIZE[1]):
        #         data[i, j] = int(str(i)+str(j))
        # dst.write(data.astype(rasterio.int16), 1)


@pytest.fixture
def default_metadata_type_docs():
    return [doc for (path, doc) in datacube.utils.read_documents(_DEFAULT_METADATA_TYPES_PATH)]


@pytest.fixture
def default_metadata_type_doc(default_metadata_type_docs):
    return [doc for doc in default_metadata_type_docs if doc['name'] == 'eo'][0]


@pytest.fixture
def telemetry_metadata_type_doc(default_metadata_type_docs):
    return [doc for doc in default_metadata_type_docs if doc['name'] == 'telemetry'][0]


@pytest.fixture
def ga_metadata_type_doc():
    _FULL_EO_METADATA = Path(__file__).parent.joinpath('extensive-eo-metadata.yaml')
    [(path, eo_md_type)] = datacube.utils.read_documents(_FULL_EO_METADATA)
    return eo_md_type


@pytest.fixture
def default_metadata_types(index, default_metadata_type_docs):
    # type: (Index, list) -> list
    for d in default_metadata_type_docs:
        index.metadata_types.add(index.metadata_types.from_doc(d))
    return index.metadata_types.get_all()


@pytest.fixture
def ga_metadata_type(index, ga_metadata_type_doc):
    return index.metadata_types.add(index.metadata_types.from_doc(ga_metadata_type_doc))


@pytest.fixture
def default_metadata_type(index, default_metadata_types):
    return index.metadata_types.get_by_name('eo')


@pytest.fixture
def telemetry_metadata_type(index, default_metadata_types):
    return index.metadata_types.get_by_name('telemetry')


@pytest.fixture
def indexed_ls5_scene_dataset_types(index, ga_metadata_type):
    """
    :type index: datacube.index._api.Index
    :rtype: datacube.model.StorageType
    """

    dataset_types = load_test_dataset_types(
        DATASET_TYPES / 'ls5_scenes.yaml',
        # Use our larger metadata type with a more diverse set of field types.
        metadata_type=ga_metadata_type
    )

    types = []
    for dataset_type in dataset_types:
        types.append(index.products.add_document(dataset_type))

    return types


@pytest.fixture
def example_ls5_nbar_metadata_doc():
    return load_yaml_file(_EXAMPLE_LS5_NBAR_DATASET_FILE)[0]


def load_test_dataset_types(filename, metadata_type=None):
    types = load_yaml_file(filename)
    return [alter_dataset_type_for_testing(type_, metadata_type=metadata_type) for type_ in types]


def load_yaml_file(filename):
    with open(str(filename)) as f:
        return list(yaml.load_all(f, Loader=SafeLoader))


def alter_dataset_type_for_testing(type_, metadata_type=None, driver='NetCDF CF'):
    if 'measurements' in type_:
        type_ = limit_num_measurements(type_)
    if 'storage' in type_:
        storage = type_['storage']
        shrink_factors = TEST_STORAGE_SHRINK_FACTORS
        if 'driver' in storage:
            storage['driver'] = driver
            if driver in ('s3', 's3-test'):
                shrink_factors = TEST_STORAGE_SHRINK_FACTORS_S3
        if is_geogaphic(type_):
            type_ = shrink_storage_type(type_, GEOGRAPHIC_VARS, shrink_factors)
        else:
            type_ = shrink_storage_type(type_, PROJECTED_VARS, shrink_factors)

    if metadata_type:
        type_['metadata_type'] = metadata_type.name

    return type_


def limit_num_measurements(storage_type):
    measurements = storage_type['measurements']
    if len(measurements) > TEST_STORAGE_NUM_MEASUREMENTS:
        storage_type['measurements'] = measurements[:TEST_STORAGE_NUM_MEASUREMENTS]
    return storage_type


def use_test_storage(storage_type):
    storage_type['location_name'] = 'testdata'
    return storage_type


def is_geogaphic(storage_type):
    return 'latitude' in storage_type['storage']['resolution']


def shrink_storage_type(storage_type, variables, shrink_factors):
    storage = storage_type['storage']
    for var in variables:
        storage['resolution'][var] = storage['resolution'][var] * shrink_factors[0]
        storage['chunking'][var] = storage['chunking'][var] / shrink_factors[1]
    return storage_type
