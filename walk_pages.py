#!/usr/bin/env python3
# -*- mode: python; tab-width: 4; indent-tabs-mode: nil -*- for emacs

"""
A program that "walks" the main news pages on the Dutch Teletekst
service (via the JSON serving backend that the web UI itself uses)
"""

import argparse
from   html.parser import HTMLParser
import json
import re
import sys
import time
import urllib3


# The pattern for fetching the page data, this assumes we only fetch
# the first page of multi-pages
TELETEKST_DATA_URL = 'https://teletekst-data.nos.nl/json/%s-1?t=%d'


def parse_options():
    """
    Parse the options, mostly required
    """
    parser = argparse.ArgumentParser(
        description="Walk The Teletekst Pages"
    )
    parser.add_argument('-f', '--file',
                        help="Path of the file to which to save the data",
                        required=True)
    parser.add_argument('-l', '--last',
                        help="Path to the (optional) checkpoint file.",
                        required=True)
    parser.add_argument('-c', '--compare',
                        help="Path to the (optional) 'compare' file, for compare-index-pages.py.")
    return parser.parse_args()


class TextParser(HTMLParser):
    """
    Convenience class that just grabs all the text parts
    """
    def __init__(self):
        super().__init__()
        self.text_collected = ""

    def handle_data(self, data):
        self.text_collected += data

    def error(self, message):
        print(f"Parsing error: {message}")


def parse_headlines(text):
    """
    Do interpretive dance for the 101..103 index pages
    """
    headlines = []
    for line in text.split("\n"):
        line_match = re.match(r"^ (\S.*[^.])(\.*) (\d{3})$", line)
        if line_match:
            headlines.append([
                line_match.group(1), int(line_match.group(3))
            ])
    return headlines


def parse_page(text, raw):
    """
    Parse the title and the main content of the page
    """
    content = []
    lines = text.split("\n")
    title = lines[2]
    title_match = re.match(r"^=+\s+(.*)$", title)
    if title_match:
        title = title_match.group(1)
    title = title.strip()
    content = lines[4:-3]
    return [
        title,
        content,
        re.sub(r"(&#xF020;)", " ", raw)
        # Why they don't just use an actual space. Doing this
        # substitution makes the HTML rendering in
        # compare-index-pages.py generate a better image.
    ]


def fetch_page(page, http, stamp, is_index):
    """
    Fetch a single page's JSON data and either parse out the text, or
    treat it like an index page
    """
    result = http.request('GET', TELETEKST_DATA_URL % (page, stamp))
    if result.data == b'':
        return None, None

    try:
        page_json = json.loads(result.data)
    except ValueError as e:
        print(f"Error parsing JSON: {e}")
        print(result.data)
        sys.exit(1)

    content = page_json['content']

    # These are custom font characters that NOS Teletekst uses, for
    # decorative purposes, and I have no need for them
    content = re.sub(r"(&#xF0[0-9a-fA-F]{2};)+", " ", content)

    parser = TextParser()
    parser.feed(content)
    if is_index:
        page_data = parse_headlines(parser.text_collected)
    else:
        page_data = parse_page(parser.text_collected, page_json['content'])

    return page_data, [
        page_json['prevPage'],
        page_json['nextPage'],
        page_json['prevSubPage'],
        page_json['nextSubPage']
    ]


def fetch_all_pages():
    """
    Fetch the three index pages, and then walk the remaining ones with
    the nextPage references
    """
    # Cache buster
    stamp = int(time.time())

    http = urllib3.PoolManager()

    # The structure that we'll actually save to a file, at the end
    all_data = {}

    # See what pages are referenced on the index pages:
    pages = {}
    for idx in (101, 102, 103):
        page_data, page_meta = fetch_page(idx, http, stamp, True)
        # print(f'INDEX: {page_data}')
        for _, page in page_data:
            pages[page] = str(idx)

    # Now find what's actually there
    current = 104
    while True:
        page_data, page_meta = fetch_page(current, http, stamp, False)

        if current in pages:
            idx = pages[current]
            del pages[current]
            if idx not in all_data:
                all_data[idx] = []
            all_data[idx].append([
                page_data[0], current, page_data[1], page_data[2]
            ])
        else:
            # There are regularly pages in the 104..199 range that are
            # not referenced by any of the three index pages. I have
            # no idea why, but I'm tracking them just in case.
            idx = '000'
            if idx not in all_data:
                all_data[idx] = []
            all_data[idx].append([
                page_data[0], current, page_data[1], page_data[2]
            ])

        current = int(page_meta[1]) # nextPage
        if current > 199:
            break

    for page, idx in pages.items():
        page_data, page_meta = fetch_page(page, http, stamp, False)
        if not page_data:
            # Occasionally there are pages (persistently, even) listed
            # on the index pages that don't actually exist anymore.
            pass
        else:
            if idx not in all_data:
                all_data[idx] = []
            all_data[idx].append([
                page_data[0], page, page_data[1], page_data[2]
            ])
    return all_data


def main():
    """
    Parse the option, fetch the index pages, and walk any other pages
    from 104 to 199
    """
    opts = parse_options()

    all_data = fetch_all_pages()

    last_filename = None
    if opts.last:
        # Check if the checkpoint file exists.
        #
        # If it does, read it as well, and compare the data.
        #
        # If the data is identical to the current data, don't bother
        # writing the new file, or updating the checkpoint file.
        try:
            with open(opts.last, "r", encoding="utf-8") as last_f:
                last_filename = last_f.read().strip()
                last_f.close()
            with open(last_filename, "r", encoding="utf-8") as last_file:
                last_json = json.load(last_file)
            if last_json == all_data:
                # Identical news items as last time.
                return
        except IOError:
            # The checkpoint file does not exist (yet), it will in a moment
            pass
    with open(opts.file, "w", encoding="utf-8") as data_f:
        data_f.write(json.dumps(all_data))
        data_f.close()
    with open(opts.last, "w", encoding="utf-8") as last_f:
        last_f.write(opts.file)
        last_f.close()
    if opts.compare and last_filename:
        with open(opts.compare, "w", encoding="utf-8") as compare_f:
            compare_f.write(f"{last_filename}\n{opts.file}\n")
            compare_f.close()


if __name__ == "__main__":
    main()
