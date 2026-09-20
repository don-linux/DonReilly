# /// script
# dependencies = [
#   "aiohttp",
#   "lxml",
# ]
# ///

import argparse
import asyncio
import json
import zipfile

import aiohttp
from lxml import etree
from lxml import html as lhtml

BASE_URL = 'https://learning.oreilly.com'

# HTTP statuses worth retrying: rate-limiting and transient server errors.
RETRY_STATUSES = {403, 429, 500, 502, 503, 504}

CONTAINER = b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
    <rootfiles>
        <rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>
"""  # noqa


def to_xhtml(s, root_path):
    tree = lhtml.fromstring(s, parser=lhtml.HTMLParser(encoding='utf-8'))

    for el in list(tree.iter()):
        for attr in ['href', 'src']:
            if (el.get(attr) or '').startswith(root_path):
                el.set(attr, el.get(attr).removeprefix(root_path))

    if tree.tag != 'html':
        wrapper = etree.Element('html', nsmap={
            None: 'http://www.w3.org/1999/xhtml',
            'epub': 'http://www.idpf.org/2007/ops',
        })

        h1 = tree.find('.//h1')
        if h1 is not None:
            head = etree.SubElement(wrapper, 'head')
            title = etree.SubElement(head, 'title')
            title.text = ''.join(h1.itertext()).strip()

        body = etree.SubElement(wrapper, 'body')
        body.append(tree)
        tree = wrapper

    return etree.tostring(
        tree,
        xml_declaration=True,
        doctype='<!DOCTYPE html>',
        pretty_print=True,
        encoding='utf-8',
    )


async def check_auth(session):
    url = BASE_URL + '/api/v1/user-preferences/'
    async with session.get(url, raise_for_status=False) as r:
        return r.ok


async def fetch(session, url, *, retries=5, backoff=1.0):
    """GET `url` and return the body, retrying transient failures with
    exponential backoff (backoff, 2*backoff, 4*backoff, ...)."""
    for attempt in range(retries + 1):
        try:
            async with session.get(url) as r:
                return await r.read()
        except aiohttp.ClientResponseError as e:
            if e.status not in RETRY_STATUSES or attempt == retries:
                raise
            reason = f'HTTP {e.status}'
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == retries:
                raise
            reason = type(e).__name__

        wait = backoff * 2 ** attempt
        print(f'  {reason} on {url}; retry {attempt + 1}/{retries} in {wait:g}s')
        await asyncio.sleep(wait)


async def fetch_book(book_id, zfh, session, *, delay=0, concurrency=4, retries=5):
    root_path = f'/api/v2/epubs/urn:orm:book:{book_id}/files/'
    semaphore = asyncio.Semaphore(concurrency)

    async def download(url, path):
        async with semaphore:
            content = await fetch(session, url, retries=retries)
        if path.endswith(('.html', '.xhtml')):
            content = to_xhtml(content, root_path)
        zfh.writestr(path, content)

    zfh.writestr('mimetype', b'application/epub+zip', compress_type=zipfile.ZIP_STORED)
    zfh.writestr('META-INF/container.xml', CONTAINER)

    url = BASE_URL + root_path
    while url:
        print(f'fetching {url}')
        data = json.loads(await fetch(session, url, retries=retries))

        await asyncio.gather(*[
            download(result['url'], f'EPUB/{result["full_path"]}')
            for result in data.get('results', [])
        ])

        url = data.get('next')
        if url:
            await asyncio.sleep(delay)


async def amain():
    parser = argparse.ArgumentParser()
    parser.add_argument('book_id')
    parser.add_argument('--jwt')
    parser.add_argument('--delay', type=int, default=0, help=(
        'Seconds to wait between batches of file downloads. '
        'Workaround for 403 errors caused by rate-limiting.'
    ))
    parser.add_argument('--concurrency', type=int, default=4, help=(
        'Maximum number of simultaneous file downloads (default: 4).'
    ))
    parser.add_argument('--retries', type=int, default=5, help=(
        'Retries per request on 403/429/5xx or connection errors, '
        'with exponential backoff (default: 5).'
    ))
    args = parser.parse_args()

    filename = f'{args.book_id}.epub'

    with zipfile.ZipFile(filename, 'w') as zfh:
        async with aiohttp.ClientSession(
            raise_for_status=True,
            cookies={'orm-jwt': args.jwt},
        ) as session:
            if not args.jwt:
                print('No JWT provided. Continuing without…')
            elif await check_auth(session):
                print('Authentication successful.')
            else:
                print('Authentication failed. Continuing without…')

            await fetch_book(
                args.book_id, zfh, session,
                delay=args.delay,
                concurrency=args.concurrency,
                retries=args.retries,
            )

    print(f'created {filename}')


if __name__ == '__main__':
    asyncio.run(amain())
