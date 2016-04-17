from setuptools import setup

setup(name='pylgtv',
      version='0.0.2',
      description='Library to control webOS based LG Tv devices',
      url='https://github.com/TheRealLink/pylgtv',
      author='Dennis Karpienski',
      author_email='dennis@karpienski.de',
      license='MIT',
      packages=['pylgtv'],
      install_requires=['websockets', 'asyncio'],
      zip_safe=True)
