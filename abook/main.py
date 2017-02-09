import argparse
import collections
import logging
import mimetypes
import os
import pathlib
import subprocess

import attr
import tornado.ioloop
import yaml

from abook import app, const, scan, tagutils, utils

mimetypes.add_type('audio/x-m4b', '.m4b')

LOG = logging.getLogger(__name__)


def non_negative(instance, attribute, value):
    if not value >= 0:
        raise ValueError(f'{attribute.name} must be non-negative')


def lang_code(instance, attribute, value):
    if not utils.validate_lang_code(value):
        raise ValueError(f'{attribute.name} contains invalid language code')


def is_cover(artifact):
    return artifact.type == 'cover'


def is_fanart(artifact):
    return artifact.type == 'fanart'


@attr.attrs(str=False, frozen=True)
class Duration(object):
    duration = attr.attrib(
        convert=int,
        validator=non_negative,
    )

    def __str__(self):
        return utils.format_duration(self.duration)

    @classmethod
    def from_string(cls, s):
        return cls(utils.parse_duration(s))

    def _split(self):
        minutes, seconds = divmod(self.duration, 60)
        hours, minutes = divmod(minutes, 60)
        return hours, minutes, seconds

    def __format__(self, format):
        h, m, s = self._split()
        if format == 'h:m:s.ms':
            return f'{h:02d}:{m:02d}:{s:02d}.000'
        return f'{h:02d}:{m:02d}:{s:02d}'


@attr.attrs(frozen=True)
class Chapter(object):
    name = attr.attrib()
    start = attr.attrib()
    end = attr.attrib(default=Duration(0))

    @classmethod
    def from_dict(cls, d: dict):
        start = Duration.from_string(d['start'])
        end = Duration.from_string(d.get('end', '0'))
        return cls(d['name'], start, end)

    def as_dict(self) -> dict:
        return {
            'name': self.name,
            'start': str(self.start),
            'end': str(self.end),
        }


@attr.attrs
class Filelike(object):
    _path = attr.attrib(convert=pathlib.Path)
    _size = attr.attrib(init=False, default=None)

    def __repr__(self):
        return f'<{self.__class__.__name__}:{self.path}>'

    @property
    def path(self):
        return self._path

    @property
    def size(self):
        if self._size is None:
            self._size = self._path.stat().st_size
        return self._size

    @property
    def mimetype(self):
        return utils.first_of(mimetypes.guess_type(str(self.path)))

    @property
    def ext(self):
        return self._path.suffix.lstrip('.')

    def as_dict(self) -> dict:
        return {
            'path': str(self.path),
        }

    @classmethod
    def from_dict(cls, d: dict):
        return cls(d.get('path'))


@attr.attrs
class Artifact(Filelike):
    description = attr.attrib()
    type = attr.attrib(default='other')

    def as_dict(self) -> dict:
        d = super().as_dict()
        d.update({
            'description': self.description,
            'type': self.type,
        })
        return d

    @classmethod
    def from_dict(cls, d: dict):
        return cls(
            d.get('path'),
            d.get('description'),
            d.get('type'),
        )


@attr.attrs
class Audiofile(Filelike):
    author = attr.attrib()
    title = attr.attrib()
    duration = attr.attrib(default=0)
    explicit = attr.attrib(default=False)
    chapters = attr.attrib(default=attr.Factory(list), repr=False)

    @classmethod
    def from_dict(cls, d: dict):
        duration = Duration.from_string(d.get('duration', '0'))
        return cls(
            d.get('path'),
            d.get('author'),
            d.get('title'),
            duration=duration,
            chapters=[Chapter.from_dict(cd) for cd in d.get('chapters', [])],
        )

    def as_dict(self) -> dict:
        d = super().as_dict()
        d.update({
            'title': self.title,
            'author': self.author,
            'duration': str(self.duration),
            'explicit': self.explicit,
            'chapters': [c.as_dict() for c in self.chapters],
        })
        return d


@attr.attrs
class Abook(collections.abc.Sequence):
    VERSION = 1
    _filename = attr.attrib(convert=pathlib.Path)
    authors = attr.attrib()
    title = attr.attrib()
    slug = attr.attrib()
    description = attr.attrib(default=None)
    lang = attr.attrib(
        validator=lang_code,
        default=const.DEFAULT_LANG_CODE,
    )
    _audiofiles = attr.attrib(default=attr.Factory(list), repr=False)
    artifacts = attr.attrib(default=attr.Factory(list), repr=False)

    def __getitem__(self, idx):
        return self._audiofiles[idx]

    def __len__(self):
        return len(self._audiofiles)

    @property
    def path(self):
        return self._filename.parent

    @property
    def has_cover(self):
        return bool(self.covers)

    @property
    def covers(self):
        return [af for af in self.artifacts if is_cover(af)]

    @property
    def has_fanart(self):
        return bool(self.fanarts)

    @property
    def fanarts(self):
        return [af for af in self.artifacts if is_fanart(af)]

    @classmethod
    def from_dict(cls, filename, d):
        return cls(
            filename,
            d.get('authors', []),
            d.get('title'),
            d.get('slug'),
            description=d.get('description') or '',
            lang=d.get('lang', const.DEFAULT_LANG_CODE),
            audiofiles=[
                Audiofile.from_dict(ad) for ad in d.get('audiofiles', [])
            ],
            artifacts=[
                Artifact.from_dict(ad) for ad in d.get('artifacts', [])
            ],
        )

    def as_dict(self) -> dict:
        return {
            'version': Abook.VERSION,
            'authors': self.authors,
            'title': self.title,
            'slug': self.slug,
            'description': self.description or '',
            'lang': self.lang,
            'audiofiles': [
                af.as_dict() for af in self
            ],
            'artifacts': [
                af.as_dict() for af in self.artifacts
            ],
        }


def do_init(args):
    results = scan.labeled_scan(
        args.directory,
        {
            'audio': utils.audio_matcher,
            'cover': utils.cover_matcher,
            'fanart': utils.fanart_matcher,
            'image': utils.image_matcher,
        }
    )

    audio_files = sorted(results.get('audio', []))
    if not audio_files:
        raise SystemExit('No audio files found!')

    audiofiles, artifacts, authors, albums = (
        [], [], collections.OrderedDict(), collections.OrderedDict(),
    )
    for idx, item_path in enumerate(audio_files, start=1):
        abs_path = os.path.join(args.directory, item_path)
        tags = tagutils.get_tags(abs_path)
        author = tags.artist if tags.artist else 'Unknown artist'
        item = Audiofile(
            item_path,
            author=author,
            title=tags.title,
            duration=Duration(tags.duration),
        )
        if tags.album:
            albums[tags.album] = True
        authors[author] = True
        audiofiles.append(item)

    album = utils.first_of(list(albums.keys())) if albums else 'Unknown album'

    unique = set()
    for c in ('cover', 'fanart', 'image'):
        for result in results.get(c, []):
            if result in unique:
                continue
            artifacts.append(Artifact(result, c, type=c))
            unique.add(result)

    bundle = Abook(
        args.directory,
        list(authors.keys()),
        album,
        utils.slugify(album),
        audiofiles=audiofiles,
        artifacts=artifacts,
    )

    with open(os.path.join(args.directory, args.output), 'w') as f:
        yaml.dump(
            bundle.as_dict(), f,
            default_flow_style=False, indent=2, width=79)


def do_transcode(args):
    data = yaml.load(args.abook_file)
    book = Abook.from_dict(
        os.path.abspath(args.abook_file.name), data)

    output_dir = pathlib.Path(os.path.abspath(args.output_dir))

    if not book.has_cover:
        cover_filename = None
    else:
        cover_filename = book.path / utils.first_of(book.covers).path

    for af in book:
        filename = book.path / af.path
        output_filename = (output_dir / af.path).with_suffix('.opus')
        tags = tagutils.get_tags(str(filename))
        LOG.info(f'Transcoding: {af.path} to: {output_filename}...')
        lame = subprocess.Popen([
            'lame',
            '--quiet',
            '--decode',
            '--mp3input',
            str(filename),
            '-'
        ], stdout=subprocess.PIPE)
        chapter_comments = []
        for i, chapter in enumerate(af.chapters):
            chapter_comments.extend([
                '--comment',
                f'CHAPTER{i:03d}={chapter.start:hh:mm:ss.ms}',
                '--comment',
                f'CHAPTER{i:03d}NAME={chapter.name}',
            ])
        opusenc_args = [
            'opusenc',
            '--quiet',
            '--raw',
            '--raw-rate',
            str(tags.sample_rate),
            '--raw-chan',
            str(tags.channels),
            '--bitrate',
            str(args.bitrate),
            '--max-delay',
            str(args.max_delay),
            '--artist',
            af.author if af.author else ', '.join(book.authors),
            *chapter_comments,
            '--album',
            book.title,
            '--title',
            af.title,
            '-',
            output_filename,
        ]
        if cover_filename:
            opusenc_args.extend([
                '--picture',
                f'3||Front Cover||{cover_filename!s}',
            ])
        opusenc = subprocess.Popen(opusenc_args, stdin=lame.stdout)
        lame.stdout.close()
        opusenc.communicate()


def do_serve(args):
    d = yaml.load(args.abook_file)
    bundle = Abook.from_dict(
        os.path.abspath(args.abook_file.name), d)
    bapp = app.make_app(bundle)
    bapp.listen(args.port)
    tornado.ioloop.IOLoop.current().start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-d',
        '--debug',
        dest='log_level',
        action='store_const',
        const=logging.DEBUG,
        default=logging.INFO,
    )
    subparsers = parser.add_subparsers()

    init_parser = subparsers.add_parser('init')
    init_parser.add_argument('directory')
    init_parser.add_argument('output', help='abook output filename')
    init_parser.add_argument('--id')
    init_parser.set_defaults(func=do_init)

    serve_parser = subparsers.add_parser('serve')
    serve_parser.add_argument(
        'abook_file',
        type=argparse.FileType('r'),
    )
    serve_parser.add_argument('-p', '--port', type=int, default=8000)
    serve_parser.set_defaults(func=do_serve)

    transcode_parser = subparsers.add_parser('transcode')
    transcode_parser.add_argument(
        'abook_file',
        type=argparse.FileType('r'),
    )
    transcode_parser.add_argument('output_dir')
    transcode_parser.add_argument(
        '-b',
        '--bitrate',
        metavar='N.NNN',
        type=float,
        default=48.0,
        help='Target bitrate in kbit/sec (6-256/channel). '
             'Default: %(default)s',
    )
    transcode_parser.add_argument(
        '--max-delay',
        metavar='N',
        type=int,
        default=1000,
        help='Maximum container delay in milliseconds (0-1000). '
             'Default: %(default)s'
    )
    transcode_parser.set_defaults(func=do_transcode)

    args = parser.parse_args()

    logging.basicConfig(level=args.log_level)

    if hasattr(args, 'func'):
        return args.func(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
