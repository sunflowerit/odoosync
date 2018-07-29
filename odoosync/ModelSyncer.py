import logging
import netrc
import odoorpc
import ssl
import sys
import time
import urllib2
from collections import OrderedDict, defaultdict
from pprint import pprint


logger = logging.getLogger()
logger.setLevel(logging.INFO)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)


DEFAULT_EXCLUDED_FIELDS = [
    'id',
    '__last_update',
    'create_date',
    'create_uid',
    'write_date',
    'write_uid'
]

class SyncException(Exception):
    pass


class OdooInstance():
    """ Abstraction of an Odoo Instance """

    def __init__(self, odoo_instance):
        opener = False
        protocol = 'jsonrpc'

        if odoo_instance.get('ssl', None):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            opener = urllib2.build_opener(urllib2.HTTPSHandler(context=ctx))
            protocol = 'jsonrpc+ssl'

        self.host = odoo_instance.get('host')
        self.port = odoo_instance.get('port')
        self.database = odoo_instance.get('database')
        self.odoo = odoorpc.ODOO(
            self.host,
            port=self.port,
            opener=opener,
            protocol=protocol
        )
        self._login()
        self.ir_model_obj = self.odoo.env['ir.model.data']
        self._get_timestamp()

    def _login(self):
        # get login details from netRC file
        try:
            netrc_info = netrc.netrc()
        except IOError:
            raise SyncException(self.host)
        auth_info = netrc_info.authenticators(self.host)
        if not auth_info:
            raise SyncException(self.host)
        username, host2, password = auth_info
        logger.info("Connecting to host={}, database={}, user={}".format(
            self.host, self.database, username))
        self.odoo.login(self.database, username, password)

    def _get_timestamp(self):
        obj = self.ir_model_obj
        dummy_record_id = obj.create({
            'model': 'res.users',
            'module': '__sfit_export_internals',
            'name': '__sfit_timestamp_{}'.format(int(time.time())),
            'res_id': self.odoo.env.uid
        })
        dummy_record = obj.read([dummy_record_id])
        self.timestamp = dummy_record[0]['create_date']
        obj.unlink(dummy_record_id)


class OdooModel():
    """ Abstraction of an Odoo model """

    def __init__(self, model_dict):
        self.fields = []
        self.many2onefields = {}
        self.source_records = []
        self.source_record_ids = set()
        self.name = model_dict.get('model')
        self.domain = model_dict.get('domain', [])
        self.context = model_dict.get('context', {})
        excluded_fields = model_dict.get('excluded_fields')
        self.excluded_fields = set(excluded_fields or []).union(
            set(DEFAULT_EXCLUDED_FIELDS))

    def load_recs(self, odoo, _ids, dep=False):
        """ Loads extra records: [id, id, id...] """
        loaded = []
        if _ids:
            logger.info(u'Reading {} {} records from source server...'.format(
                len(_ids), self.name))
            source_obj = odoo.env[self.name]
            records = source_obj.browse(_ids).with_context({
                'mail_create_nosubscribe': True
            }).read(self.fields)
            if dep:
                for record in records:
                    record.update({'__sfit_dep': True})
            self.source_records.extend(records)
            loaded.extend(records)
            self.source_record_ids.update(set(r['id'] for r in records))
        return loaded


class ModelSyncer():
    """ Syncer instance """ 

    def __init__(self, _struct, _timestamps):
        self.manual_mapping = _struct.get('manual_mapping', {})
        self.source_timestamp = _timestamps.get('source')
        self.dest_timestamp = _timestamps.get('target')
        self.source = OdooInstance(_struct.get('source', {}))
        self.dest = OdooInstance(_struct.get('target', {}))
        self.source_ir_fields = self.source.odoo.env['ir.model.fields']
        self.options = _struct.get('options', {})
        self.dry_run = self.options.get('dry_run')
        self.models = [OdooModel(m) for m in _struct.get('models', {})]
        self.models_by_name = dict((m.name, m) for m in self.models)
        self.prefix = self.options.get(
            'prefix', '__export_sfit__').rstrip('.')
        timeout = self.options.get('timeout', 600)
        self.source.odoo.config['timeout'] = timeout
        self.dest.odoo.config['timeout'] = timeout

    def _get_xmlid(self, model_name, _id):
        return u'{}_{}'.format(model_name.replace('.', '_'), _id)

    def _create_dest_mapping(self):
        # Create a translation dict for dest_record_id -> source record id
        self.dest_trans = defaultdict(dict)
        xmlids = []
        for model in self.models:
            for source_id in list(model.source_record_ids):
                xmlids.append(self._get_xmlid(model.name, source_id))
        dest_external_ids = self.dest.ir_model_obj.search([
            ('name', 'in', xmlids),
            ('module', '=', self.prefix)
        ])
        dest_external_records = self.dest.ir_model_obj.browse(
            dest_external_ids
        ).read(['name', 'model', 'res_id'])
        for r in dest_external_records:
            model_name = r['model']
            dest_id = r['res_id']
            try:
                source_id = int(str(r['name'].split('.')[-1]).split('_')[-1])
                self._add_dest_id(model_name, source_id, dest_id)
            except ValueError:
                pass

    def _find_dest_id(self, model_name, source_id):
        return source_id and self.dest_trans.get(model_name, {}).get(source_id, {})

    def _add_dest_id(self, model_name, source_id, dest_id):
        self.dest_trans[model_name][source_id] = dest_id

    def _map_fields(self, model, data):
        vals = data.copy()
        for field, rel_model in model.many2onefields.iteritems():
            source_id = data.get(field) and data.get(field)[0]
            if source_id:
                logger.debug("Translating {}[{}]...".format(rel_model, source_id))
                dest_id = self._find_dest_id(rel_model, source_id)
                if dest_id:
                    logger.debug(str(dest_id))
                    vals[field] = dest_id
                else:
                    dest_id = self.manual_mapping.get(rel_model, {}).get(source_id)
                    logger.debug(str(dest_id))
                    if dest_id:
                        vals[field] = dest_id
                    else:
                        logger.warning('Mapping failed: consider adding manual mapping for record {}[{}]'.format(
                            rel_model, source_id))
                        vals[field] = None
        return vals

    def _write_or_create_model_record(self, model, vals, source_id, noupdate=False):
        dest_id = self._find_dest_id(model.name, source_id)
        xmlid = self._get_xmlid(model.name, source_id)
        if not dest_id:
            # double check
            record = self.dest.ir_model_obj.search([
                ('module', '=', self.prefix),
                ('name', '=', xmlid),
            ])
            if record:
                dest_id = record[0]
                self._add_dest_id(model.name, source_id, dest_id)
        if dest_id and not noupdate:
            logger.info(u'updating record {}[{}] from source {}'.format(
                model.name, dest_id, source_id))
            try:
                if not self.dry_run:
                    record = self.dest.odoo.env[model.name].write(dest_id, vals)
            except odoorpc.error.RPCError:
                dest_id = None
                record = self.dest.ir_model_obj.search([
                    ('module', '=', self.prefix),
                    ('name', '=', xmlid)
                ])
                self.dest.ir_model_obj.unlink(record)
        if not dest_id:
            logger.info(u'creating record from source {}[{}]..'.format(
                model.name, source_id))
            if not self.dry_run:
                dest_id = self.dest.odoo.env[model.name].create(vals)
                logger.info(str(dest_id))
                self._add_dest_id(model.name, source_id, dest_id)
                self.dest.ir_model_obj.create({
                    'model': model.name,
                    'name': xmlid,
                    'res_id': dest_id,
                })

    def _sync_one_model(self, model):
        logger.debug(u'Totally {} {} records considered for sync...'.format(
            len(model.source_records), model.name))
        for source_record in model.source_records:
            source_id = source_record['id']
            noupdate = bool(source_record.get('__sfit_dep'))
            mapped = self._map_fields(model, source_record)
            self._write_or_create_model_record(
                model, mapped, source_id, noupdate=noupdate)

    #def _load_dependencies_of_records(self, model, records):
    #    dep_struct = defaultdict(set)
    #    many2onefields = self._many2one_fields.get(model, {})
    #    for record in records:
    #        for field, rel_model in many2onefields.iteritems():
    #            source_id = record.get(field) and record.get(field)[0]
    #            existing_records = self._source_record_ids.get(rel_model, [])
    #            if source_id and source_id not in existing_records:
    #                dep_struct[rel_model].add(source_id)
    #    for _model, _records in self._load_records(dep_struct,
    #            extra_fields={'__sfit_dep': True}).iteritems():
    #        self._load_dependencies_of_records(_model, _records)

    def _load_dependencies_of_records(self, model, records):
        dep_struct = defaultdict(set)
        for record in records:
            for field, rel_model_name in model.many2onefields.iteritems():
                rel_model = self.models_by_name.get(rel_model_name)
                source_id = record.get(field) and record.get(field)[0]
                if rel_model:
                    source_id = record.get(field) and record.get(field)[0]
                    if source_id and source_id not in rel_model.source_record_ids:
                        dep_struct[rel_model_name].add(source_id)
                else:
                    logger.debug('Ignoring {}[{}]', rel_model_name, source_id)
        for _model_name, _ids in dep_struct.iteritems():
            _model = self.models_by_name[_model_name]
            _records = _model.load_recs(self.source.odoo, _ids, dep=True)
            self._load_dependencies_of_records(_model, _records)

    def prepare(self):
        for model in self.models:
            logger.info(u'Preparing sync of model {}'.format(model.name))

            # Determine which fields to sync exactly, per model
            logger.debug("Determining which fields to sync...")
            fields = self.source_ir_fields.search([('model', '=', model.name)])
            fields_info = self.source_ir_fields.browse(fields).read([])
            for field in fields_info:
                name = field.get('name')
                relation = field.get('relation')
                ttype = field.get('ttype')
                readonly = field.get('readonly')
                # Skip computed fields
                if readonly:
                    continue
                # Skip excluded fields, one2many fields, many2many fields
                if name in model.excluded_fields or (relation and ttype != 'many2one'):
                    continue
                model.fields.append(name)
                if relation and ttype == 'many2one':
                    model.many2onefields[name] = relation

            # Fetch source records
            source_obj = self.source.odoo.env[model.name]
            domain = model.domain
            if self.source_timestamp:
                domain.append(('write_date', '>', self.source_timestamp))
            logger.info(u'Searching: {}'.format(domain))
            self.source.odoo.context = model.context
            _ids = source_obj.search(domain or [])
            model.load_recs(self.source.odoo, _ids)

        # determine dependencies
        for model in self.models:
            logger.info(u'Determine dependent records for {}...'.format(model.name))
            records = model.source_records
            self._load_dependencies_of_records(model, records)

        # Sort records so that parents always come before children
        for model in self.models:
            records = model.source_records
            if records and 'parent_id' in records[0].keys():
                logger.info(u'Sorting records according to parent-child hierarchy...')
                def _sort(todo, ids_done):
                    done = []
                    more_ids_done = []
                    still_todo = []
                    for _record in todo:
                        parent_id = _record.get('parent_id')
                        if not parent_id or parent_id in ids_done:
                            done.append(_record)
                            more_ids_done.append(_record['id'])
                        else:
                            still_todo.append(_record)
                    if more_ids_done:
                        done.extend(_sort(still_todo, ids_done + more_ids_done))
                    else:
                        done.extend(still_todo)
                    return done
                source_records = _sort(records, [])
            else:
                source_records = records
            model.source_records = source_records

        logger.info("Creating a mapping of ids of already synced records from target server...")
        self._create_dest_mapping()

    def sync(self):
        for model in self.models:
            self._sync_one_model(model)

    def get_new_timestamps(self):
        if not self.dry_run:
            return {
                'source': self.source.timestamp,
                'target': self.dest.timestamp
            }
