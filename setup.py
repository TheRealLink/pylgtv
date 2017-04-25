from distutils.core import setup

setup(
      name = 'pylgtv',
      packages = ['pylgtv'],
      package_dir = {'pylgtv': 'pylgtv'},
      package_data = {'pylgtv': ['handshake.json']},
      install_requires = ['websockets', 'asyncio'],
      zip_safe = True,
      version = '0.1.7',
      description = 'Library to control webOS based LG Tv devices',
      author = 'Dennis Karpienski',
      author_email = 'dennis@karpienski.de',
      url = 'https://github.com/TheRealLink/pylgtv',
      download_url = 'https://github.com/TheRealLink/pylgtv/archive/0.1.6.tar.gz',
      keywords = ['webos', 'tv'],
      classifiers = [],
)