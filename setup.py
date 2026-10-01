from setuptools import setup, find_packages

setup(
    name='labgoblin',
    version='2.0.0',
    packages=find_packages(include=['labgoblin', 'labgoblin.*']),
    include_package_data=False,
    install_requires=[
        'rich',
        'markdown-it-py>=3.0',
        'tomli_w',
        'psutil>=5.9',
        'github-copilot-sdk==1.0.15',
        'pywin32>=306; sys_platform == "win32"',
    ],
    package_data={
        'labgoblin': ['static/*.css', 'static/*.js'],
    },
    data_files=[('share/labgoblin/examples/local-synthetic', [
        'examples/local-synthetic/README.md', 'examples/local-synthetic/research_goal.md',
        'examples/local-synthetic/experiment.py', 'examples/local-synthetic/batch.json',
        'examples/local-synthetic/Dockerfile',
    ])],
    extras_require={
        'docker-build': ['docker==7.1.0'],
    },
    entry_points={
        'console_scripts': [
            'labgoblin=labgoblin.cli:main',
        ],
    },
    author='Roger Creus Castanyer',
    author_email='creus99@gmail.com',
    description='LabGoblin: local autonomous research with Claude and GitHub Copilot, derived from xgenius',
    long_description=open('README.md', encoding='utf-8').read(),
    long_description_content_type='text/markdown',
    url='https://github.com/tupini07/LabGoblin',
    project_urls={'Original upstream': 'https://github.com/roger-creus/xgenius'},
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.11',
)
