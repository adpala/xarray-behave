from setuptools import setup, find_packages
import os

# read the contents of your README file
this_directory = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(this_directory, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()

setup(name='xarray_behave',
      version='0.2',
      description='xarray_behave',
      long_description=long_description,
      long_description_content_type="text/markdown",
      url='http://github.com/janclemenslab/xarray-behave',
      author='Jan Clemens',
      author_email='clemensjan@googlemail.com',
      license='MIT',
      packages=find_packages('src'),
      package_dir={'': 'src'},
      install_requires=['PySide2', 'numpy', 'scipy', 'xarray', 'h5py', 'zarr', 'flammkuchen', 'pysoundfile'],
      extras_require={'gui': ['pyside2', 'pyqtgraph', 'simpleaudio', 'scikit-image', 'opencv-python']},
      include_package_data=True,
      zip_safe=False
     )
