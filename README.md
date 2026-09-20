# DonReilly

Don Reilly is a Python script that lets you download O'Reilly books as EPUB files

## Create a Python virtual environment

```bash
python3 -m venv donreilly
source donreilly/bin/activate
```

## Install the dependencies

```bash
pip install aiohttp lxml
```

## Get your orm-jwt

1.- Open [O'Reilly Learning](https://learning.oreilly.com/) in your browser

2.- Open DevTools and go to the Cookies section

3.- Search for the orm-jwt cookie

![orm-jwt cookie](orm-jwt.png)

4.- Then copy the orm-jwt token

## Download your book

```bash
python3 donreilly.py <book-id> --jwt '<orm-jwt-token>'
```
