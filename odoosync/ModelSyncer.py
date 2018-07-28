import logging
import odoorpc
import sys
import time
from collections import OrderedDict, defaultdict
from pprint import pprint


logger = logging.getLogger()
logger.setLevel(logging.INFO)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
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


class ModelSyncer():

    def __init__(self, source, dest, manual_mapping=None):
        self.manual_mapping = manual_mapping or {}
        self.source = source
        self.dest = dest
        self.source_ir_fields = source.env['ir.model.fields']
        self.dest_ir_model_obj = dest.env['ir.model.data']
        self._prefix = '__export_sfit__.'

    def _get_proper_name(self, model, _id):
        return u'{}_{}'.format(model.replace('.', '_'), _id)

    def _load_records(self, _struct, extra_fields=None):
        """ Loads records from a {'model.name': [id, id, id]} format """
        loaded = defaultdict(list)
        for model, _ids in _struct.iteritems():
            if _ids:
                logger.info(u'Reading {} {} records from source server...'.format(
                    len(_ids), model))
                model_fields = self._fields_struct.get(model, [])
                source_obj = self.source.env[model]
                records = source_obj.browse(_ids).with_context({
                    'mail_create_nosubscribe': True
                }).read(model_fields)
                if extra_fields:
                    for record in records:
                        record.update(extra_fields)
                self._source_records[model].extend(records)
                loaded[model].extend(records)
                self._source_record_ids[model].update(set(r['id'] for r in records))
        return loaded

    def _create_dest_mapping(self):
        # Create a translation dict for dest_record_id -> source record id
        self.dest_trans = defaultdict(dict)
        proper_names = []
        for model, source_ids in self._source_record_ids.iteritems():
            for source_id in list(source_ids):
                proper_names.append(
                    self._prefix +
                    self._get_proper_name(model, source_id)
                )
        dest_external_ids = self.dest_ir_model_obj.search([
            ('name', 'in', proper_names)
        ])
        dest_external_records = self.dest_ir_model_obj.browse(
            dest_external_ids
        ).read(['name', 'model', 'res_id'])
        for r in dest_external_records:
            model = r['model']
            dest_id = r['res_id']
            try:
                source_id = int(str(r['name'].split('.')[-1]).split('_')[-1])
                self._add_dest_id(model, source_id, dest_id)
            except ValueError:
                pass

    def _find_dest_id(self, model, source_id):
        return source_id and self.dest_trans.get(model, {}).get(source_id, {})

    def _add_dest_id(self, model, source_id, dest_id):
        self.dest_trans[model][source_id] = dest_id

    def _get_proper_name(self, model, id):
        return u'{}_{}'.format(model.replace('.', '_'), id)

    def _map_fields(self, model, data):
        vals = data.copy()
        for field, rel_model in self._many2one_fields.get(model, {}).iteritems():
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
        dest_id = self._find_dest_id(model, source_id)
        proper_name = self._prefix + self._get_proper_name(model, source_id)
        if not dest_id:
            # double check
            record = self.dest_ir_model_obj.search([('name', '=', proper_name)])
            if record:
                dest_id = record[0]
                self._add_dest_id(model, source_id, dest_id)
        if dest_id and not noupdate:
            logger.info(u'updating record {}[{}] from source {}'.format(model, dest_id, source_id))
            try:
                record = self.dest.env[model].write(dest_id, vals)
            except odoorpc.error.RPCError:
                dest_id = None
                record = self.dest_ir_model_obj.search([('name', '=', proper_name)])
                self.dest_ir_model_obj.unlink(record)
        if not dest_id:
            logger.info(u'creating record from source {}[{}]..'.format(model, source_id))
            dest_id = self.dest.env[model].create(vals)
            logger.info(str(dest_id))
            self._add_dest_id(model, source_id, dest_id)
            self.dest_ir_model_obj.create({
                'model': model,
                'name': proper_name,
                'res_id': dest_id,
            })

    def _sync_one_model(self, model):
        source_records = self._source_records.get(model, [])
        logger.debug(u'Totally {} {} records considered for sync...'.format(
            len(source_records), model))
        for source_record in source_records:
            source_id = source_record['id']
            noupdate = bool(source_record.get('__sfit_dep'))
            mapped = self._map_fields(model, source_record)
            self._write_or_create_model_record(
                model, mapped, source_id, noupdate=noupdate)

    def _load_dependencies_of_records(self, model, records):
        dep_struct = defaultdict(set)
        many2onefields = self._many2one_fields.get(model, {})
        for record in records:
            for field, rel_model in many2onefields.iteritems():
                source_id = record.get(field) and record.get(field)[0]
                existing_records = self._source_record_ids.get(rel_model, [])
                if source_id and source_id not in existing_records:
                    dep_struct[rel_model].add(source_id)
        for _model, _records in self._load_records(dep_struct,
                extra_fields={'__sfit_dep': True}).iteritems():
            self._load_dependencies_of_records(_model, _records)

    def prepare(self, model_dicts, since=None):
        self._fields_struct = defaultdict(list)
        self._many2one_fields = defaultdict(dict)
        self._source_records = defaultdict(list)
        self._source_record_ids = defaultdict(set)
        self._depends_struct = defaultdict(set)  # dependent records per model

        for model_dict in model_dicts:
            domain = model_dict.get('domain', [])
            context = model_dict.get('context', {})
            model = model_dict.get('model')
            self.source.env.context.update(context)
            self.dest.env.context.update(context)
            logger.info(u'Preparing sync of model {}'.format(model))

            # Determine which fields to sync exactly, per model
            logger.debug("Determining which fields to sync...")
            excluded_fields = model_dict.get('excluded_fields')
            excluded_fields_set = set(excluded_fields or []).union(
                set(DEFAULT_EXCLUDED_FIELDS))
            fields = self.source_ir_fields.search([('model', '=', model)])
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
                if name in excluded_fields_set or (relation and ttype != 'many2one'):
                    continue
                self._fields_struct[model].append(name)
                if relation and ttype == 'many2one':
                    self._many2one_fields[model][name] = relation

            # Fetch source records
            source_obj = self.source.env[model]
            if since:
                domain.append(('write_date', '>', since))
            logger.info(u'Searching: {}'.format(domain))
            _ids = source_obj.search(domain or [])
            self._load_records({model: _ids})

        # determine dependencies
        self._depends_struct = defaultdict(set)  # dependent records per model
        for model_dict in model_dicts:
            model = model_dict.get('model')
            logger.info(u'Determine dependent records for {}...'.format(model))
            records = self._source_records.get(model, [])
            self._load_dependencies_of_records(model, records)

        # Sort records so that parents always come before children
        for model_dict in model_dicts:
            model = model_dict.get('model')
            records = self._source_records.get(model, [])
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
            self._source_records[model] = source_records

        logger.info("Creating a mapping of ids of already synced records from target server...")
        self._create_dest_mapping()

    def sync(self, model_dict):
        model = model_dict.get('model')
        self._sync_one_model(model)

    def get_timestamp(self):
        obj = self.dest_ir_model_obj
        dummy_record_id = obj.create({
            'model': 'res.user',
            'name': '__sfit_timestamp_{}'.format(int(time.time())),
            'res_id': self.dest.env.uid
        })
        timestamp = obj.read(dummy_record_id)[0]['create_date']
        obj.unlink(dummy_record_id)
        return timestamp
 
