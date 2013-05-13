# Copyright 2011 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import setuptools
import sys

from novaclient.openstack.common import setup


def read_file(file_name):
    return open(os.path.join(os.path.dirname(__file__), file_name)).read()
project = 'python-novaclient'

setuptools.setup(
    name=project,
    version=setup.get_version(project),
    author='OpenStack',
    author_email='openstack-dev@lists.openstack.org',
    description="Client library for OpenStack Compute API.",
    long_description=read_file("README.rst"),
    license="Apache License, Version 2.0",
    url="https://github.com/openstack/python-novaclient",
    packages=setuptools.find_packages(exclude=['tests', 'tests.*']),
    install_requires=setup.parse_requirements(),
    cmdclass=setup.get_cmdclass(),
    setup_requires=['setuptools_git>=0.4'],
    include_package_data=True,
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Environment :: Console",
        "Environment :: OpenStack",
        "Intended Audience :: Developers",
        "Intended Audience :: Information Technology",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
        "Programming Language :: Python"
    ],
    entry_points={
        "console_scripts": ["nova = novaclient.shell:main"]
    },
)
