from setuptools import setup
from distutils.util import convert_path

main_ns = {}
ver_path = convert_path('rebase/version.py')
with open(ver_path) as ver_file:
    exec(ver_file.read(), main_ns)

setup(
    name='rebase-toolkit',
    url='https://github.com/rebaseenergy/rebase-toolkit',
    packages=['rebase'],
    install_requires=['requests>=2.20.0', 'pandas>=1.0.0', 'dill', 'PyYAML', 'dvc[azure]', 'mlflow', 'joblib', 'click'],
    include_package_data=True,
    version=main_ns['__version__'],
    license='Apache 2.0',
    description='Rebase Python toolkit',
    long_description=open('README.md').read(),
    entry_points={
        'console_scripts': [
            'rebase=rebase.cli.main:main'
        ]
    }
)
