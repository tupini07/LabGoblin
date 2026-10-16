from setuptools import setup, find_packages

setup(
    name='labgoblin',
    version='3.0.0',
    packages=find_packages(include=['labgoblin', 'labgoblin.*']),
    include_package_data=False,
    package_data={'labgoblin': ['inspection_assets/*.html', 'inspection_assets/*.css', 'inspection_assets/*.js']},
    install_requires=[
        'tomli_w',
        'psutil>=5.9',
        'github-copilot-sdk==1.0.15',
        'pywin32>=306; sys_platform == "win32"',
    ],
    data_files=[('share/labgoblin/examples/local-synthetic', [
        'examples/local-synthetic/README.md', 'examples/local-synthetic/research_goal.md',
        'examples/local-synthetic/experiment.py',
    ])],
    entry_points={
        'console_scripts': [
            'labgoblin=labgoblin.cli:main',
        ],
    },
    author='Roger Creus Castanyer',
    author_email='creus99@gmail.com',
    description='LabGoblin: a persistent Copilot researcher with optional stopping review, derived from xgenius',
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
