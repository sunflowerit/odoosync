import hashlib
import logging
import netrc
import odoorpc
import ssl
import sys
import time
import urllib2
from collections import OrderedDict, defaultdict
from pprint import pprint


logger = logging.getLogger(__name__)
ch = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)
logger.setLevel(logging.INFO)
ch.setLevel(logging.INFO)


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
        self.records = []
        self.record_ids = set()
        self.name = model_dict.get('model')
        self.domain = model_dict.get('domain', [])
        self.no_domain = model_dict.get('no_domain')
        self.context = model_dict.get('context', {})
        self.excluded_fields = set(model_dict.get('excluded_fields', [])) \
            .union(set(DEFAULT_EXCLUDED_FIELDS))
        self.included_fields = set(model_dict.get('included_fields', []))
        self.reverse = bool(model_dict.get('reverse'))
        self.trans = {}
        self.translatable_ids = set()

    def load_recs(self, odoo, _ids, dep=False):
        """ Loads records into this model: [id, id, id...] """
        loaded = []
        if _ids:
            logger.info(u'Reading {} {} records from server...'.format(
                len(_ids), self.name))
            source_obj = odoo.env[self.name]
            records = source_obj.read(_ids, self.fields)
            if dep:
                for record in records:
                    record.update({'__sfit_dep': True})
            self.records.extend(records)
            loaded.extend(records)
            self.record_ids.update(set(r['id'] for r in records))
        return loaded

    def sort_parents_before_children(self):
        """ Sort records so that parents are before children.
            This is important when filling parent_id for children in an import
            session where the parent record is also created in that session """
        records = self.records
        if records and 'parent_id' in records[0].keys():
            logger.info(u'Sorting {} according to parent-child hierarchy...'.format(self.name))
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
            sorted_records = _sort(records, [])
        else:
            sorted_records = records
        self.records = sorted_records

    def determine_fields(self, odoo, dest_odoo, other_models, mapping):
        """ Determine which field to sync for this model """
        logger.debug("Determining which fields to sync for {}".format(self.name))
        source_ir_fields = odoo.env['ir.model.fields']
        dest_ir_fields = dest_odoo.env['ir.model.fields']
        fields_domain = [('model', '=', self.name)]
        if self.included_fields:
            fields_domain.append(('name', 'in', list(self.included_fields)))
        field_ids = source_ir_fields.search(fields_domain)
        fields = source_ir_fields.read(field_ids, [])
        dest_field_ids = dest_ir_fields.search(fields_domain)
        dest_fields = dest_ir_fields.read(dest_field_ids, ['name'])
        dest_field_names = [r['name'] for r in dest_fields]
        other_model_names = set([m.name for m in other_models])
        other_model_names = set(mapping.keys()).union(other_model_names)
        for field in fields:
            name = field.get('name')
            relation = field.get('relation')
            ttype = field.get('ttype')
            readonly = field.get('readonly')
            # Skip computed fields
            if readonly:
                continue
            # Skip excluded fields, one2many fields, many2many fields
            if name in self.excluded_fields or (relation and ttype != 'many2one'):
                continue
            # Skip fields that dont exist on dest
            if name not in dest_field_names:
                logger.warning("Field {}[{}] does not exist on "
                    "destination, consider mapping to "
                    "another field".format(self.name, name))
                continue
            if relation and ttype == 'many2one':
                # Skip many2one fields with a relation that we
                # are not including in this sync
                if relation not in other_model_names:
                    continue
                self.many2onefields[name] = relation
            self.fields.append(name)
        logger.debug("{}".format(self.fields))

    def _map_fields(self, data, find_dest_id_function):
        mapped = data.copy()
        for field, rel_model_name in self.many2onefields.iteritems():
            source_id = data.get(field) and data.get(field)[0]
            if source_id:
                dest_id = find_dest_id_function(rel_model_name, source_id)
                if not dest_id:
                    logger.warning("Mapping failed: consider adding "
                        "manual mapping for record {}[{}]".format(
                        rel_model_name, source_id))
                mapped[field] = dest_id or None
        return mapped


class ModelSyncer():
    """ Syncer instance """ 

    def __init__(self, _struct, _timestamps):
        self.options = _struct.get('options', {})
        self.dry_run = self.options.get('dry_run')
        self.debug = self.options.get('debug')
        
        if self.debug:
            logger.setLevel(logging.DEBUG)
            ch.setLevel(logging.DEBUG)
        logger.info('-----------START-----------')
        logger.debug('Created ModelSyncer instance...')
        self.manual_mapping = _struct.get('manual_mapping', {})
        self.reverse_manual_mapping = \
            _struct.get('reverse_manual_mapping', {})
        self.source_timestamp = _timestamps.get('source')
        self.dest_timestamp = _timestamps.get('target')
        self.source = OdooInstance(_struct.get('source', {}))
        self.dest = OdooInstance(_struct.get('target', {}))
        self.source_ir_fields = self.source.odoo.env['ir.model.fields']
        self.models = [
            OdooModel(m) for m in _struct.get('models', {})
            if not m.get('reverse')]
        self.models_by_name = \
            dict((m.name, m) for m in self.models)
        self.reverse_models = [
            OdooModel(m) for m in _struct.get('models', {})
            if m.get('reverse')]
        self.reverse_models_by_name = \
            dict((m.name, m) for m in self.reverse_models)
        self.prefix = self.options.get(
            'prefix', '__export_sfit__').rstrip('.')
        timeout = self.options.get('timeout', 600)
        self.source.odoo.config['timeout'] = timeout
        self.dest.odoo.config['timeout'] = timeout

    def _get_xmlid(self, model_name, _id):
        return u'{}_{}'.format(model_name.replace('.', '_'), _id)

    def create_xmlid(self, model_name, source_id, dest_id):
        """ Create a link between source id and dest id """
        xmlid = self._get_xmlid(model_name, source_id)
        self.dest.ir_model_obj.create({
            'model': model_name,
            'module': self.prefix,
            'name': xmlid,
            'res_id': dest_id,
        })

    def create_reverse_xmlid(self, model_name, source_id, dest_id):
        """ Create a link between dest id and source id """
        xmlid = self._get_xmlid(model_name, dest_id)
        self.dest.ir_model_obj.create({
            'model': model_name,
            'module': self.prefix,
            'name': xmlid,
            'res_id': source_id,
        })

    def _add_translations(self, loaded):
        """ Create translation tables for source record id -> dest record id
            loaded is a {model.name -> [ids]} dict, to translate."""
        xmlids = []
        for model_name, _ids in loaded.iteritems():
            for source_id in list(_ids):
                xmlids.append(self._get_xmlid(model_name, source_id))
        dest_external_ids = self.dest.ir_model_obj.search([
            ('name', 'in', xmlids),
            ('module', '=', self.prefix)
        ])
        dest_external_records = self.dest.ir_model_obj.read(
            dest_external_ids, ['name', 'model', 'res_id'])
        for r in dest_external_records:
            try:
                source_id = int(str(r['name'].split('.')[-1]).split('_')[-1])
                self._add_dest_id(r['model'], source_id, r['res_id'])
            except ValueError:
                pass
        for model in self.models:
            logger.debug("Model {} with {} translations".format(model.name,
                len(model.trans)))

    def _add_reverse_translations(self, loaded):
        """ Create translation tables for dest record id -> source record id
            loaded is a {model.name -> [ids]} dict, to translate."""
        dest_external_ids = []
        for model_name, _ids in loaded.iteritems():
            # TODO: this may be a very slow search, and there are several
            # for each model. An optimization could be to query the source
            # server, and to store the reverse xmlids also there.
            dest_external_ids += self.dest.ir_model_obj.search([
                ('res_id', 'in', list(_ids)),
                ('model', '=', model_name),
                ('module', '=', self.prefix)
            ])
        dest_external_records = self.dest.ir_model_obj.read(
            dest_external_ids, ['name', 'model', 'res_id'])
        for r in dest_external_records:
            try:
                source_id = int(str(r['name'].split('.')[-1]).split('_')[-1])
                self._add_source_id(r['model'], r['res_id'], source_id)
            except ValueError:
                pass

    def _translate_to_dest_id(self, model_name, source_id, get_xmlid=False):
        xmlid = self._get_xmlid(model_name, source_id)
        record = self.dest.ir_model_obj.search([
            ('module', '=', self.prefix),
            ('name', '=', xmlid),
        ])
        if record:
            if get_xmlid:
                return record
            return self.dest.ir_model_obj.read(record, ['res_id'])[0]['res_id']
        else:
            return None

    def _translate_to_source_id(self, model_name, dest_id, get_xmlid=False):
        record = self.dest.ir_model_obj.search([
            ('module', '=', self.prefix),
            ('res_id', '=', dest_id),
            ('model', '=', model_name),
        ])
        if record:
            if get_xmlid:
                return record
            xmlid = self.dest.ir_model_obj.read(record, ['name'])[0]['name']
            try:
                source_id = int(xmlid.split('.')[-1].split('_')[-1])
            except ValueError:
                source_id = None
            return source_id
        else:
            return None

    def _find_dest_id(self, model_name, source_id):
        model = self.models_by_name.get(model_name)
        dest_id = self.manual_mapping.get(model_name, {}).get(source_id)
        if not dest_id:
            dest_id = source_id and model and model.trans.get(source_id)
        logger.debug("source {}[{}] -> dest {}[{}]".format(
            model_name, source_id, model and model.name, dest_id))
        return dest_id

    def _find_source_id(self, model_name, dest_id):
        model = self.reverse_models_by_name.get(model_name)
        source_id = self.reverse_manual_mapping.get(
            model_name, {}).get(dest_id)
        if not source_id:
            source_id = dest_id and model and model.trans.get(dest_id)
        logger.debug("dest {}[{}] -> source {}[{}]".format(
            model_name, dest_id, model and model.name, source_id))
        return source_id

    def _add_dest_id(self, model_name, source_id, dest_id):
        model = self.models_by_name.get(model_name)
        if model:
            model.trans[source_id] = dest_id
            model.translatable_ids.add(source_id)

    def _add_source_id(self, model_name, dest_id, source_id):
        model = self.reverse_models_by_name.get(model_name)
        if model:
            model.trans[dest_id] = source_id
            model.translatable_ids.add(dest_id)

    def _make_hash(self, vals):
        _hash = hashlib.md5()
        for k, v in sorted(vals.iteritems()):
            if k == 'id':
                continue
            if isinstance(v, (list, tuple)):
                v = v[0]
            if not v:
                v = '___None'
            _hash.update(unicode(v).encode('utf-8'))
        return _hash.hexdigest()

    def _write_or_create_model_record(
            self, 
            odoo,
            model,
            vals, 
            source_id, 
            find_dest_id_function,
            add_dest_id_function, 
            create_xmlid_function,
            translate_function,
            noupdate=False):
        dest_id = find_dest_id_function(model.name, source_id)
        logger.debug('cache {}->{} ({})'.format(
            source_id, dest_id, model.name))
        if not dest_id:
            # double check
            dest_id = translate_function(model.name, source_id)
            logger.debug('trans {}->{} ({})'.format(
                source_id, dest_id, model.name))
            if dest_id:
                add_dest_id_function(model.name, source_id, dest_id)
        obj = odoo.odoo.env[model.name]
        if dest_id and not noupdate:
            # check if changed
            old_vals = obj.read([dest_id], model.fields)[0]
            if self._make_hash(old_vals) == self._make_hash(vals):
                logger.info(u'no change: not updating {}[{}] from {}'.format(
                    model.name, dest_id, source_id))
            else:
                logger.info(u'updating record {}[{}] from source {}'.format(
                    model.name, dest_id, source_id))
                try:
                    if not self.dry_run:
                        record = obj.write(dest_id, vals)
                except odoorpc.error.RPCError as e:
                    logger.error('Writing {}[{}] failed: {}'.format(
                        model.name, dest_id, str(e)))
                    # TODO: How to deal with this
        if not dest_id:
            logger.info(u'creating record from source {}[{}]..'.format(
                model.name, source_id))
            if not self.dry_run:
                try:
                    dest_id = obj.create(vals)
                    logger.info(str(dest_id))
                    add_dest_id_function(model.name, source_id, dest_id)
                    create_xmlid_function(model.name, source_id, dest_id)
                except odoorpc.error.RPCError as e:
                    logger.error('Creating {} failed: {}'.format(
                        model.name, str(e)))
                    # TODO: How to deal with this

    def _sync_one_model(self, model):
        logger.debug(u'Totally {} {} records considered for sync...'.format(
            len(model.records), model.name))

        if model.reverse:
            find_dest_id_function = self._find_source_id
            add_dest_id_function = self._add_source_id
            create_xmlid_function = self.create_reverse_xmlid
            translate_function = self._translate_to_source_id
            odoo = self.source
        else:
            find_dest_id_function = self._find_dest_id
            add_dest_id_function = self._add_dest_id
            create_xmlid_function = self.create_xmlid
            translate_function = self._translate_to_dest_id
            odoo = self.dest
        obj = odoo.odoo.env[model.name]

        to_update = {}
        to_create = []
        dest_ids = []
        new_hashes = {}
        for record in model.records:
            source_id = record['id']
            if bool(record.get('__sfit_dep')) \
                    or not source_id in model.translatable_ids:
                to_create.append((source_id, record))
            else:
                mapped = model._map_fields(
                    record,
                    find_dest_id_function)
                dest_id = find_dest_id_function(model.name, source_id)
                dest_ids.append(dest_id)
                _hash = self._make_hash(mapped)
                to_update[dest_id] = (source_id, _hash, mapped)

        # Create records
        if to_create:
            logger.info('{} records to create'.format(len(to_create)))
        for source_id, record in to_create:
            logger.info(u'creating record from source {}[{}]..'.format(
                model.name, source_id))
            mapped = model._map_fields(
                record,
                find_dest_id_function)
            if not self.dry_run:
                try:
                    dest_id = obj.create(mapped)
                    logger.info(str(dest_id))
                    add_dest_id_function(model.name, source_id, dest_id)
                    create_xmlid_function(model.name, source_id, dest_id)
                except odoorpc.error.RPCError as e:
                    logger.error('Creating {} failed: {}'.format(
                        model.name, str(e)))
                    # TODO: How to deal with this

        really_update = []
        if dest_ids:
            logger.info('{} records to update'.format(len(to_update)))
            logger.info('Checking hashes...')
            really_update = []
            old_vals = obj.read(dest_ids, model.fields)
            for vals in old_vals:
                old_hash = self._make_hash(vals)
                dest_id = vals['id']
                source_id, new_hash, new_vals = to_update[dest_id]
                if old_hash != new_hash:
                    really_update.append((source_id, dest_id, new_vals))
        
        # update
        if really_update:
            logger.info('{} records are changed'.format(len(really_update)))
        for source_id, dest_id, vals in really_update:
            logger.info(u'updating record {}[{}] from source {}'.format(
                model.name, dest_id, source_id))
            try:
                if not self.dry_run:
                    record = obj.write(dest_id, vals)
            except odoorpc.error.RPCError as e:
                logger.error('Writing {}[{}] failed: {}'.format(
                    model.name, dest_id, str(e)))
                # TODO: How to deal with this

        #self._write_or_create_model_record(
        #    odoo,
        #    model,
        #    mapped,
        #    source_id, 
        #    find_dest_id_function,
        #    add_dest_id_function,
        #    create_xmlid_function,
        #    translate_function,
        #    noupdate=noupdate)

    def _load_dependencies_of_records(self, odoo, loaded, other_models,
            add_translations):
        """ loaded: dict of {'model.name' -> records} 
            the records, as well as the othermodels[*].records
            can be considered as those that should surely be loaded """
        count = sum(len(recs) for m, recs in loaded.iteritems())
        if not count:
            return
        logger.info(u'Find dependencies for {} records...'.format(count))
        dep_struct = defaultdict(set)
        ignore_struct = defaultdict(set)
        for model_name, records in loaded.iteritems():
            model = other_models[model_name]
            for record in records:
                for field, rel_model_name in model.many2onefields.iteritems():
                    _id = record.get(field) and record.get(field)[0]
                    rel_model = other_models.get(rel_model_name)
                    if not rel_model:
                        # We dont deal with this model at all
                        ignore_struct[rel_model_name].add(_id)
                    elif _id and _id in rel_model.record_ids:
                        # It is already loaded
                        continue
                    elif _id:
                        # we will have figure out whether to load it,
                        # or whether just to translate it.
                        dep_struct[rel_model_name].add(_id)
        _loaded = {}
        add_translations(dep_struct)
        for _model_name, _ids in dep_struct.iteritems():
            _model = other_models[_model_name]
            ids_to_load = _ids - _model.translatable_ids
            recs = _model.load_recs(odoo, list(ids_to_load), dep=True)
            _loaded[_model_name] = recs
        self._load_dependencies_of_records(odoo, _loaded, other_models,
            add_translations)
        for _model_name, _ids in ignore_struct.iteritems():
            logger.debug('Ignoring {}{}'.format(_model_name, str(_ids)))

    def prepare(self):
        syncs = [
            (self.source.odoo, self.dest.odoo, 
             self.source_timestamp, self.models, False,
             self.manual_mapping,
             self.models_by_name,
             self._add_translations),
            (self.dest.odoo, self.source.odoo,
             self.dest_timestamp, self.reverse_models, True,
             self.reverse_manual_mapping,
             self.reverse_models_by_name,
             self._add_reverse_translations)
        ]
        for odoo, dest_odoo, since, models, reverse, mapping,\
                models_by_name, add_translations in syncs:
            logger.info("-----------PREPARE {}SYNC"
                "----------".format("REVERSE " if reverse else ""))
            # Determine field names
            for model in models:
                logger.info("Determine fields for model {}...".format(model.name))
                model.determine_fields(odoo, dest_odoo, models, mapping)

            # Load records
            for model in models:
                if model.no_domain:
                    continue
                domain = model.domain
                if since:
                    domain.append(('write_date', '>', since))
                logger.info(u'Searching: {} {}'.format(model.name, domain))
                odoo.context = model.context
                _ids = odoo.env[model.name].search(domain or [])
                logger.debug(u'Found: {}'.format(str(_ids)))
                model.load_recs(odoo, _ids)

            # Translate loaded records
            trans = dict((m.name, m.record_ids) for m in models)
            add_translations(trans)

            # Determine and load dependencies
            loaded = dict((m.name, m.records) for m in models)
            self._load_dependencies_of_records(odoo, loaded, 
                models_by_name, add_translations)

            # Sort loaded records so that parents always come before children
            for model in models:
                model.sort_parents_before_children()

    def sync(self):
        logger.info('-----------NORMAL SYNC-----------')
        for model in self.models:
            self._sync_one_model(model)
        logger.info('-----------REVERSE SYNC----------')
        for model in self.reverse_models:
            self._sync_one_model(model)
        logger.info('-----------END-----------')

    def get_new_timestamps(self):
        if not self.dry_run:
            return {
                'source': self.source.timestamp,
                'target': self.dest.timestamp
            }
