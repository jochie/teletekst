#!/usr/bin/env python3
# -*- mode: python; tab-width: 4; indent-tabs-mode: nil -*- for emacs

"""
Compare the data collected about Teletekst pages from two timestamps,
and post as needed to a Mastodon server using a token.
"""

# I've got work to do, and I know it:
#
# pylint: disable=line-too-long, too-many-locals, too-many-branches
# pylint: disable=too-many-arguments, too-many-statements

# The token requires access to these scopes:
# - read:statuses
# - write:media
# - write:statuses

import argparse

# For showing differences between two pages
import difflib

import io
import json
import os
import re
import sys
import urllib3


# apt-get install python3-packaging
#
# to check the particular version of PIL/pillow, so I know whether to
# use textbbox() or textsize()
from packaging.version import Version

# apt-get install python3-pil
import PIL
from PIL import Image, ImageDraw, ImageFont

# For rendering the Teletekst page into an image, for uploading:
#
# pip3 install imgkit (not installed by default)
# also needs the appropriate version from https://wkhtmltopdf.org/downloads.html
import imgkit

# Constants used by generate_diff_attachment()
TAB_SIZE = 4
FNT_SIZE = 15


def parse_options():
    """
    Parse the option, do some interpretive dance around them
    """
    parser = argparse.ArgumentParser(description='Compare two snapshots of ' +
                                     'the Teletekst news pages, and see what '+
                                     'is new, disappeared, or moved around.')
    parser.add_argument('-c', '--compare',
                        help="Path to the 'compare' file that 'fetch-index-pages.py' may write.")
    parser.add_argument('-p', '--prev',
                        help="Path of the previous list of Teletekst news pages")
    parser.add_argument('-n', '--next',
                        help="Path of the next list of Teletekst news pages")
    parser.add_argument('-u', '--update',
                        help="Update the 'last' symlink",
                        default=False, action='store_true')
    parser.add_argument('--post',
                        help="Post about (probably) new pages to Mastodon - "+
                        "requires --server and --token",
                        default=False, action='store_true')
    parser.add_argument('-s', '--server',
                        help="Mastodon server hostname")
    parser.add_argument('-t', '--token',
                        help="Token for posting to the Mastodon server")
    parser.add_argument('--state',
                        help="File in which to track the posts for news items.",
                        required=True)
    parser.add_argument("--debug",
                        help="Enable additional debug output.",
                        default=False, action='store_true')
    parser.add_argument('--dryrun',
                        help="Dryrun. Do compare the files but do not create or update posts.",
                        default=False, action='store_true')

    opts = parser.parse_args()
    if opts.compare:
        try:
            with open(opts.compare, 'r', encoding='utf-8') as compare_f:
                opts.prev = compare_f.readline().strip()
                opts.next = compare_f.readline().strip()
                compare_f.close()
        except IOError:
            # print(f"No compare file, nothing to do.")
            sys.exit(0)
        os.unlink(opts.compare)
    else:
        if not opts.prev or not opts.next:
            print("If --compare isn't used, we need both --prev and --next.")
            sys.exit(1)

    if opts.post:
        if not opts.server or not opts.token:
            print("The --post option requires that you also use the --server and --token options.")
            sys.exit(1)
        if opts.token[0] == '@':
            try:
                with open(opts.token[1:], 'r', encoding='utf-8') as token_f:
                    opts.token = token_f.read().strip()
                    token_f.close()
            except IOError:
                print(f"Token file not found: {opts.token[1:]}")
                sys.exit(1)

    return opts


def load_post_state(opts):
    """
    Load the information about which post is associated with the
    (active) page/title entries
    """
    try:
        with open(opts.state, 'r', encoding="utf-8") as state_f:
            data = json.loads(state_f.read())
            state_f.close()
            return data
    except IOError:
        return {}


def save_post_state(opts, state):
    """
    Save the post information
    """
    with open(opts.state, 'w', encoding="utf-8") as state_f:
        state_f.write(json.dumps(state))
        state_f.close()


def render_teletekst(fname, content):
    """
    Take the HTML snippet that we received from the API, wrap it in
    some more HTML so that it will render correctly, and then performa
    that magic trick with imgkit/wkhtmltoimage
    """
    # Add /usr/local/bin, if it isn't there yet, because that's where
    # the needed binary may be hiding:
    #
    # $ which wkhtmltoimage
    # /usr/local/bin/wkhtmltoimage

    path_match = re.match(r".*:/usr/local/bin:.*", f":{os.environ['PATH']}:")
    if not path_match:
        os.environ['PATH'] = os.environ['PATH'] + ":/usr/local/bin/"

    # An alternative would be to explicitly tell imgkit where it
    # lives, and possibly be wrong there anyway

    # Wrap it in the minimally needed HTML
    content = '''
<html>
  <head>
    <style>body { background: #202020; }</style>
  </head>
  <body>
    <link href="https://cdn.nos.nl/assets/nos-symfony/ceb1651/bundles/nossite/css/teletekst.css" media="all" type="text/css" rel="stylesheet"/>
    <pre class="teletekst__content__pre">%s</pre>
  </body>
</html>''' % (content,)
    imgkit.from_string(
        content,
        fname,
        options={
            # No unnecesary output, please
            'quiet': '',
            # Only the relevant content
            'crop-x': 15,
            'crop-y': 25,
            'crop-h': 440,
            'crop-w': 385,
            # Output options
            'format': 'png',
        }
    )
    # Now there should be a <fname> (PNG, JPG) file for this page


def generate_diff_attachment(opts, http, auth, text, timestamp):
    """
    Take the diff, generated earlier, and create an image out of it,
    colorizing it along the way. The initial image is purposely bigger
    than it needs to be and we'll crop it if needed.

    When that is done, upload the image to the server, returning the
    media ID that it returns.
    """
    img = Image.new('RGB', (385, 440  * 2), "#202020")
    fnt = ImageFont.truetype('DejaVuSansMono', FNT_SIZE)
    d = ImageDraw.Draw(img)

    offset = 0
    for line in text.splitlines():
        line = line.rstrip()
        while True:
            tab_m = re.match(r"^([^\t]+)\t(.*)", line)
            if not tab_m:
                break
            tab_len = len(tab_m.group(1))
            if tab_len % TAB_SIZE == 0:
                tab_len = TAB_SIZE
            else:
                tab_len = TAB_SIZE - tab_len % TAB_SIZE
            if opts.debug:
                print(f"tab_len = {tab_len}")
            line = tab_m.group(1) + "        "[:tab_len]  + tab_m.group(2)

        if re.match(r"^--- ", line) or re.match(r"^\+\+\+ ", line):
            color="yellow"
        elif re.match(r"^@@ ", line):
            color="white"
        elif re.match(r"^\+ ", line):
            color="green"
        elif re.match(r"^- ", line):
            color="red"
        else:
            color="cyan"
        if len(line) == 0:
            line = " "
        d.text((0, offset), line, fill=color, font=fnt)
        if Version(PIL.__version__) >= Version("10.0.0"):
            # left, top, right, bottom
            (_, _, _, bottom) = d.textbbox((5, offset), line, font=fnt)
            offset = bottom + 1
        else:
            # width, height = d.textsize(line, fnt)
            # print(f"Width x Height = {width} x {height}")
            offset += FNT_SIZE + 3
    if opts.debug:
        print(f"offset is now {offset}")
    if offset > 440:
        img = img.crop((0, 0, 385, offset))
    else:
        img = img.crop((0, 0, 385, 440))
    s = io.BytesIO()
    img.save(s, 'png')
    img_data = s.getvalue()
    result = http.request("POST", f"https://{opts.server}/api/v2/media",
                          headers=auth,
                          fields={
                              "file": ("tt/diff.png", img_data),
                              "description": f"[{timestamp}]\n\n{text}",
                              "focus": "0.0,1.0"
                          })
    if result.status >= 400:
        print(f"generate_diff_attachment: {result.status} {result.data}")
        sys.exit(1)
    return json.loads(result.data)['id']


def generate_attachment(opts, http, auth,
                        pagenr, raw_content, text_content,
                        timestamp):
    """
    Take a teletekst page (raw HTML snippet, text, etc), create an
    image out of it, and then upload it to the server, returning the
    media ID that it returns
    """
    # Generate the picture:
    file_name = f"tt/tt{pagenr}.png"
    render_teletekst(file_name, raw_content)

    # Prepare the attachment:
    with open(file_name, "rb") as fp:
        file_data = fp.read()
    result = http.request("POST", f"https://{opts.server}/api/v2/media",
                          headers=auth,
                          fields={
                              "file": (file_name, file_data),
                              "description": f"[{timestamp}]\n\n{text_content}"
                          })
    if result.status >= 400:
        print(f"generate_attachment: {result.status} {result.data}")
        sys.exit(1)
    return json.loads(result.data)['id']


def create_post(opts, http, auth, title, pagenr, raw_content, text_content, timestamp):
    """
    Create a post for a new Teletekst page
    """
    media_id = generate_attachment(opts, http, auth,
                                   pagenr, raw_content, text_content,
                                   timestamp)
    result = http.request(
        "POST",
        f"https://{opts.server}/api/v1/statuses",
        headers=auth,
        fields={
            'status':      f"[{pagenr}] {title}\nhttps://nos.nl/teletekst/{pagenr} #teletekst",
            'media_ids[]': media_id,
            'language':    'nl'
        })
    post_id = json.loads(result.data)['id']
    return media_id, post_id


def get_media_data(opts, http, auth, post_id):
    """
    Get the list of media IDs currently attached to the post
    """
    result = http.request(
        "GET",
        f"https://{opts.server}/api/v1/statuses/{post_id}",
        headers=auth)

    # print(result.status, result.data)
    json_data = json.loads(result.data)

    if 'media_attachments' not in json_data:
        return []
    media_data = json_data['media_attachments']
    if len(media_data) == 0:
        return []
    media_ids = []
    for entry in media_data:
        media_ids.append(entry['id'])
    return media_ids


# https://stackoverflow.com/questions/76081685/post-list-field-in-urllib3
#
def create_update(opts, http, auth, post_id, old_pagenr, old_pagedata, # pylint: disable=unused-argument
                  new_pagenr, new_pagedata, timestamp):
    """
    Update an existing post or, if the content changed, create a
    follow-up post to the original post, with a new version attached
    as well as the "diff" between the two version.
    """
    if old_pagedata['text'] == new_pagedata['text']:
        media_ids = get_media_data(opts, http, auth, post_id)
        fields = [
            ('status', f"[{new_pagenr}] {new_pagedata['title']}\nhttps://nos.nl/teletekst/{new_pagenr} #teletekst"),
            ('language', 'nl')
        ]
        for media_id in media_ids:
            fields.append(('media_ids[]', media_id))

        result = http.request("PUT",
                              f"https://{opts.server}/api/v1/statuses/{post_id}",
                              headers=auth,
                              fields=fields)
        return post_id,  result.status

    media_ids = []
    media_ids.append(generate_attachment(opts, http, auth,
                                         new_pagenr, new_pagedata['raw'], new_pagedata['text'],
                                         timestamp))
    media_ids.append(generate_diff_attachment(opts, http, auth,
                                              generate_diff(opts, old_pagedata['text'], new_pagedata['text']),
                                              timestamp))
    fields = [
        ('status', f"[{new_pagenr}] {new_pagedata['title']}\nhttps://nos.nl/teletekst/{new_pagenr} #teletekst"),
        ('language', 'nl'),
        ('in_reply_to_id', post_id)
    ]
    for media_id in media_ids:
        fields.append(('media_ids[]', media_id))
    result = http.request("POST",
                          f"https://{opts.server}/api/v1/statuses",
                          headers=auth,
                          fields=fields)
    post_id = json.loads(result.data)['id']
    return post_id, result.status


def mark_deleted(opts, http, auth, post_id, title):
    """
    Update a post to reflect the fact that a Teletekst page is no longer there
    """
    media_ids = get_media_data(opts, http, auth, post_id)
    fields = [
        ('status',   f"[Verwijderd] {title}\n#teletekst"),
        ('language', 'nl')
    ]
    for media_id in media_ids:
        fields.append(('media_ids[]', media_id))

    result = http.request("PUT",
                          f"https://{opts.server}/api/v1/statuses/{post_id}",
                          headers=auth,
                          fields=fields)
    return result.status


# Take the nested pile of information and create a single layer
# mapping as follows:
#
# {
#   <pagenr>: {
#     'title': <title>,
#     'raw':   <raw-content>,
#     'text':  <extracted-text>
#   },
#   ...
# }
def normalize_data(data):
    """
    Flatten one layer of the nested data that walk_pages.py collects,
    taking out duplicates
    """
    result = {}
    seen = {}
    for index_page in data:
        for entry in data[index_page]:
            title = entry[0]
            pagenr = entry[1]

            if title in seen:
                if seen[title] < pagenr:
                    continue
                # use the lowest pagenr, if we have dupes
                del result[seen[title]]
                del seen[title]
            seen[title] = pagenr
            result[pagenr] = {
                'title': title,
                'text':  "\n".join(entry[2]),
                'raw':   entry[3]
            }

    return result


def generate_word_map(text):
    """
    Break the text into chunks between blocks of whitespace or '.',
    then count each of the lowercased words
    """
    result = {}
    for word in re.split(r"[\s\.]+", text):
        word = word.lower()
        if word not in result:
            result[word] = 0
        result[word] += 1
    return result


def compare_word_maps(map1, map2):
    """
    Count how many words are the same between two maps generated
    previously, and compare to the total number of words
    """
    total = 0
    same = 0
    for word, count in map1.items():
        total += count
        if word not in map2:
            continue
        if count == map2[word]:
            same += count
        elif count < map2[word]:
            same += count
        else:
            same += map2[word]

    # Do the same in reverse, to catch an entirely new section being added
    for word, count in map2.items():
        total += count
        if word not in map1:
            continue
        if count == map1[word]:
            same += count
        elif count < map1[word]:
            same += count
        else:
            same += map1[word]

    # Declare it sufficiently similar if 90% of the words are the same
    return same * 100 / total >= 90, same, total


def find_matching_page(opts, data, page_map):
    """
    For a given page, find a sufficiently-matching page in the newer
    data-set
    """
    map1 = None
    for pagenr, pagedata in page_map.items():
        if pagedata['title'] == data['title'] and not re.match(r"Kort nieuws (binnen|buiten)land", data['title']):
            # Take a chance, but not for the two Kort nieuws pages.
            return pagenr

        if not map1:
            # Generate when needed
            map1 = generate_word_map(data['text'])

        map2 = generate_word_map(pagedata['text'])
        comparison, overlap, total = compare_word_maps(map1, map2)
        if opts.debug:
            print(f"{data['title']} vs {pagedata['title']} -> {comparison}, {overlap} / {total}")
        if comparison:
            # Close enough in text
            return pagenr
    return None


def create_timestamp(filename):
    """
    Take the filename of the data, which encodes the timestamp, and
    reformat that into a kinder format, chopping off the seconds.
    """
    time_m = re.match(r"pages/(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)\.json", filename)
    if not time_m:
        return "0000-00-00 00:00"
    return f"{time_m.group(1)}-{time_m.group(2)}-{time_m.group(3)} {time_m.group(4)}:{time_m.group(5)}"


def remove_extra_spaces(lines):
    """
    Remove any trailing empty lines, if there are any
    """
    result = []
    lines.reverse()
    do_add = False
    for line in lines:
        line = line.rstrip()
        if not do_add:
            if len(line) > 0:
                do_add = True
        if do_add:
            result.append(line)
    result.reverse()
    return result


def generate_diff(opts, text1, text2):
    """
    Generate a so-called unified diff comparison between the two
    versions of the Teletekst page
    """
    list1 = remove_extra_spaces(text1.split("\n"))
    list2 = remove_extra_spaces(text2.split("\n"))
    udiff = "\n".join(difflib.unified_diff(list1, list2,
                                           "Vorige versie", "Huidige versie",
                                           create_timestamp(opts.prev),
                                           create_timestamp(opts.next),
                                           n=1, lineterm=""))
    return udiff


def get_state(state, title, pagenr):
    """
    Find which post ID, if any, is associated with the given title and
    pagenr combination
    """
    pagenr = str(pagenr)
    if title in state:
        if pagenr in state[title]:
            return state[title][pagenr]
    return None


def clear_state(state, title, pagenr):
    """
    Clear the post ID for the given title and pagenr combination
    """
    pagenr = str(pagenr)
    if title in state:
        if pagenr in state[title]:
            del state[title][pagenr]
            if len(state[title]) == 0:
                del state[title]


def set_state(state, title, pagenr, post_id):
    """
    Set a post ID for the given title and pagenr combination
    """
    pagenr = str(pagenr)
    if title not in state:
        state[title] = {}
    state[title][pagenr] = post_id


def main():
    """
    Main program, which is already in need of some refactoring
    """

    opts = parse_options()

    state = load_post_state(opts)

    with open(opts.prev, 'r', encoding='utf-8') as prev_fd:
        prev_json = json.load(prev_fd)
    with open(opts.next, 'r', encoding='utf-8') as next_fd:
        next_json = json.load(next_fd)

    # Prepare for possible PUT and POST actions
    http = urllib3.PoolManager()
    if opts.token:
        auth = {'Authorization': f"Bearer {opts.token}"}
    else:
        auth = {}

    pages_alt = [] # Altered pages, pagenr, title, content, or a combination
    pages_del = [] # Pages that disappeared
    pages_new = [] # Newly created pages

    prev_list = normalize_data(prev_json)
    next_list = normalize_data(next_json)

    for pagenr, pagedata in prev_list.items():
        pagenr_match = find_matching_page(opts, pagedata, next_list)
        if not pagenr_match:
            # This page has left the building

            post_id = get_state(state, pagedata['title'], pagenr)
            if opts.dryrun:
                result_status = "<unknown>"
            else:
                result_status = mark_deleted(opts, http, auth, post_id, pagedata['title'])
                clear_state(state, pagedata['title'], pagenr)

            pages_del.append(f"#{pagenr} '{pagedata['title']}' - Post ID {post_id} ({result_status})")
            continue

        if pagenr == pagenr_match and pagedata['title'] == next_list[pagenr_match]['title'] and pagedata['text'] == next_list[pagenr_match]['text']:
            # Same page, same title, same content
            del next_list[pagenr_match]
            continue

        result_status = "<?>"
        post_id = get_state(state, pagedata['title'], pagenr)
        if post_id:
            if opts.dryrun:
                result_status = "<unknown>"
            else:
                post_id, result_status = create_update(opts, http, auth, post_id,
                                                       pagenr, pagedata,
                                                       pagenr_match, next_list[pagenr_match],
                                                       create_timestamp(opts.next))
                clear_state(state, pagedata['title'], pagenr)
                set_state(state, next_list[pagenr_match]['title'], pagenr_match, post_id)
        else:
            if opts.dryrun:
                media_id = "<unknown>"
                post_id = "<unknown>"
            else:
                media_id, post_id = create_post(opts, http, auth,
                                                next_list[pagenr_match]['title'], pagenr_match,
                                                next_list[pagenr_match]['raw'], next_list[pagenr_match]['text'],
                                                create_timestamp(opts.next)) # fill in the blank
                set_state(state, next_list[pagenr_match]['title'], pagenr_match, post_id)

        if pagenr == pagenr_match:
            sect_pagenr = f"#{pagenr}"
        else:
            sect_pagenr = f"#{pagenr} -> {pagenr_match}"

        sect_changed = []
        sect_title = f"'{pagedata['title']}'"
        if pagedata['title'] == next_list[pagenr_match]['title']:
            sect_title_extra = ""
        else:
            sect_changed.append('title')
            sect_title_extra = f" ('{next_list[pagenr_match]['title']}')"

        if pagedata['text'] == next_list[pagenr_match]['text']:
            sect_content = ""
        else:
            sect_changed.append('content')
            sect_content = generate_diff(opts, pagedata['text'], next_list[pagenr_match]['text'])
            sect_content = f":\n\n{sect_content}\n"

        if len(sect_changed) > 0:
            sect_changes = " & ".join(sect_changed)
            sect_changes = f" {sect_changes} changed"
        else:
            sect_changes = ""
        if post_id:
            sect_post = f"Post ID: {post_id} {result_status}"
        else:
            sect_post = f"Post ID <unknown> {result_status}"
        pages_alt.append(f"{sect_pagenr} {sect_title}{sect_changes}{sect_title_extra} {sect_post}{sect_content}")
        del next_list[pagenr_match]

    # Whatever is left must be new pages, then:
    for pagenr, pagedata in next_list.items():
        if opts.post:
            if opts.dryrun:
                media_id = "<unknown>"
                post_id = "<unknown>"
            else:
                media_id, post_id = create_post(
                    opts, http, auth,
                    pagedata['title'], pagenr,
                    pagedata['raw'], pagedata['text'],
                    create_timestamp(opts.next)
                )
                set_state(state, pagedata['title'], pagenr, post_id)
            pages_new.append(f"#{pagenr} '{pagedata['title']} - Media ID {media_id}; Post ID {post_id}")
        else:
            pages_new.append(f"#{pagenr} '{pagedata['title']}")

    if len(pages_del) > 0:
        print("The following page(s) no longer exist(s):")
        for line in pages_del:
            print(f"    {line}")
        print()
    if len(pages_new) > 0:
        print("New page(s) found:")
        for line in pages_new:
            print(f"    {line}")
        print()
    if len(pages_alt) > 0:
        print("The following page(s) change(d):")
        for line in pages_alt:
            print(f"    {line}")
        print()

    save_post_state(opts, state)
    return 0

if __name__ == "__main__":
    sys.exit(main())
