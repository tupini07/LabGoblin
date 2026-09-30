from setuptools import setup, find_packages

setup(
    name='xgenius',
    version='1.0.0',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'rich',
        'markdown-it-py>=3.0',
        'paramiko',
        'scp',
        'tomli_w',
        'psutil>=5.9',
        'pywin32>=306; sys_platform == "win32"',
    ],
    package_data={
        'xgenius': ['sbatch_templates/*', 'static/*.css', 'static/*.js'],
    },
    extras_require={
        'dashboard-chat': ['github-copilot-sdk==1.0.15'],
    },
    entry_points={
        'console_scripts': [
            'xgenius=xgenius.cli:main',
        ],
    },
    author='Roger Creus Castanyer',
    author_email='creus99@gmail.com',
    description='LLM-oriented autonomous research platform for SLURM clusters',
    long_description=open('README.md', encoding='utf-8').read(),
    long_description_content_type='text/markdown',
    url='https://github.com/roger-creus/xgenius',
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.11',
)
