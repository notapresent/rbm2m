# -*- coding: utf-8 -*-
"""
    Record export functions
"""
from __future__ import unicode_literals
import datetime
import logging

from sqlalchemy import func, or_
import jinja2
from jinja2.filters import do_truncate

from ..models import Scan, Record, Genre, RecordFlag, Image, scan_records
from . import user_settings, export_manager
from rbm2m.action import genre_manager

BATCH_SIZE = 10000


logger = logging.getLogger(__name__)


class Exporter(object):
    """
        Base class for exports. Builds record sets for export.
    """
    fmt = 'base'

    def __init__(self, session):
        self.session = session
        self.settings = user_settings.UserSettings(session)

    def log_export(self, ip, user_agent):
        """
            Save export entry and emit log message
        """
        expman = export_manager.ExportManager(self.session)
        expdata = {
            'user_agent': user_agent,
            'ip': ip,
            'format': self.fmt
        }
        exp = expman.from_dict(expdata)
        message = "{} export #{} for {}@{} completed"
        logger.info(message.format(self.fmt, exp.id, user_agent, ip))
        return exp

    def latest_scans(self):
        """
            List of ids of last successful scans for each export-enabled genre
        """
        subquery = (
            self.session.query(Scan.id)
                .filter(Scan.status == 'success')
                .filter(Scan.genre_id == Genre.id)
                .order_by(Scan.started_at.desc())
                .limit(1)
                .as_scalar()
        )
        rows = (
            self.session.query(Genre.id, subquery)
                .filter(subquery.isnot(None))
                .filter(Genre.export_enabled.is_(True))
                .all()
        )
        return [scan_id for genre_id, scan_id in rows]

    def records(self, scan_ids):
        """
            Returns all records from scans in scan_ids, excluding the ones with
            'missing_images' and 'skip' status

            :param scan_ids: list of scan ids
            :return: generator producing Record values
        """
        batch_no = 0
        while True:
            records = (
                self.session.query(
                    scan_records.c.record_id.label('id'),
                    Record.artist, Record.title,
                    Record.label, Record.notes, Record.grade, Record.format,
                    Record.price, Record.genre_id,
                    Genre.title.label('genre_title'),
                    func.group_concat(Image.id, ' ').label('images'))
                .join(Record, Record.id == scan_records.c.record_id)
                .join(Genre, Genre.id == Record.genre_id)
                .outerjoin(Image, Image.record_id == scan_records.c.record_id)
                .outerjoin(RecordFlag,
                           RecordFlag.record_id == scan_records.c.record_id)
                .filter(scan_records.c.scan_id.in_(scan_ids))
                .filter(or_(
                    RecordFlag.name.is_(None),
                    ~RecordFlag.name.in_(['skip', 'missing_images'])))
                .order_by(scan_records.c.record_id)
                .group_by(scan_records.c.record_id)
                .offset(batch_no * BATCH_SIZE).limit(BATCH_SIZE).all())

            if not records:
                break

            for row in records:
                yield dict(zip(row.keys(), row))

            batch_no += 1

    def category_list(self):
        """
            List of exported categories
        """
        genman = genre_manager.GenreManager(self.session)
        return genman.exported_list()


class YMLExporter(Exporter):
    fmt = 'yml'

    def __init__(self, session):
        super(YMLExporter, self).__init__(session)
        self.limit = self.export_limit()
        env = jinja2.Environment()
        template_string = self.settings['yml_description_template']['value']
        self.description_template = env.from_string(template_string)

    def export_limit(self):
        lim = self.settings['yml_export_limit']['value']
        return int(lim) if lim else False

    def generation_date(self):
        """
            Export set build date
        """
        return datetime.datetime.utcnow()

    def offers(self):
        """
            Generates a sequence of offers for YML export
        """
        scans = self.latest_scans()

        for num, rec in enumerate(self.records(scans)):
            if self.limit and num == self.limit:
                break

            yield self.make_offer(rec)

    def make_offer(self, rec):
        """
            Generates offer for YML from result row dictionary
        """
        offer = rec.copy()
        offer['title'] = format_title(rec['artist'], rec['title'], rec['format'])
        offer['description'] = self.format_description(rec)
        offer['price'] = self.make_yml_price(rec['price'])
        offer['images'] = self.format_yml_images(rec['images'])
        print offer
        return offer

    def format_description(self, rec):
        """
            Format lot description accordint to template
        """
        return self.description_template.render(**rec)

    def make_yml_price(self, price):
        """
            Calculate lot price
        """
        formula = self.settings['formula_yml']['value']
        return eval(formula, {'x': price})

    def format_yml_images(self, image_ids):
        """
            Accepts list of space-separated image ids, returns list of image paths
        """
        rv = []
        if not image_ids:
            return rv

        for img_id in image_ids.replace(' ', '').split(','):
            rv.append(Image(id=img_id).make_filename('.jpg'))

        return rv


class TableExporter(Exporter):
    fmt = 'table'

    def rows(self):
        """
            Generates a sequence of rows for table export
        """
        scans = self.latest_scans()

        for num, rec in enumerate(self.records(scans)):
            yield self.make_row(rec)

    def make_row(self, rec):
        """
            Make table row from query result row
        """

        rec['price'] = self.make_price(rec['price'])
        return rec

    def make_price(self, price):
        """
            Calculate record price according to formula
        """
        formula = self.settings['formula_table']['value']
        return eval(formula, {'x': price})


def format_title(artist, title, fmt, max_length=50):
    """
        Format offer title string according to format
        {artist} - {title} {format}
        truncating title if necessary
    """
    title_maxlength = max_length - len("{} -  {}".format(artist, fmt))
    truncated_title = do_truncate(title, title_maxlength)
    return '{} - {} {}'.format(artist, truncated_title, fmt)
