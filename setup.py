from setuptools import setup, find_packages
import ombott_ng

setup(
    name="ombott-ng",
    version=ombott_ng.__version__,
    url="https://github.com/KellerKev/ombott-ng",
    license=ombott_ng.__license__,
    author=ombott_ng.__author__,
    author_email="valq7711@gmail.com",
    maintainer="KellerKev",
    maintainer_email="kellerkev@gmail.com",
    description="One More BOTTle",
    platforms="any",
    keywords='python webapplication',
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Web Environment",
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Topic :: Internet :: WWW/HTTP :: HTTP Servers",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    python_requires='>=3.7',
    packages=find_packages('.'),
    package_data={'ombott': ['error.html']}
)
