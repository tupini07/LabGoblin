from setuptools import setup, find_packages

setup(
    name='xgenius',
    version='2.0.0',
    packages=find_packages(include=['xgenius', 'xgenius.*']),
    include_package_data=False,
    install_requires=[
        'rich',
        'markdown-it-py>=3.0',
        'tomli_w',
        'psutil>=5.9',
        'pywin32>=306; sys_platform == "win32"',
    ],
    package_data={
        'xgenius': ['static/*.css', 'static/*.js'],
    },
    data_files=[('share/xgenius/examples/local-synthetic', [
        'examples/local-synthetic/README.md', 'examples/local-synthetic/research_goal.md',
        'examples/local-synthetic/experiment.py', 'examples/local-synthetic/batch.json',
        'examples/local-synthetic/Dockerfile',
    ])],
    extras_require={
        'dashboard-chat': ['github-copilot-sdk==1.0.15'],
        'docker-build': ['docker==7.1.0'],
    },
    entry_points={
        'console_scripts': [
            'xgenius=xgenius.cli:main',
        ],
    },
    author='Roger Creus Castanyer',
    author_email='creus99@gmail.com',
    description='Local autonomous research harness for Claude and GitHub Copilot',
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
