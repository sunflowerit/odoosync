import sys
import time
from collections import OrderedDict, defaultdict
from pprint import pprint


DEFAULT_EXCLUDED_FIELDS = [
    'id',
    '__last_update',
    'create_date',
    'create_uid',
    'write_date',
    'write_uid'
]


class ModelSyncer():

    def _log(self, text, cr=True):
        print text,
        if cr:
            print
        sys.stdout.flush()

    def __init__(self, source, dest, manual_mapping=None):
        self.manual_mapping = manual_mapping or {}
        self.source = source
        self.dest = dest
        self.source_ir_fields = source.env['ir.model.fields']
        self.dest_ir_model_obj = dest.env['ir.model.data']
        self._depends_struct = OrderedDict()
        self._fields_struct = defaultdict(list)  # relevant fields per model
        self._prefix = '__export_sfit__.'
        self._log("Creating a mapping of ids of already synced records from target server...", cr=False)
        self._create_dest_mapping()
        self._log("done")

    def _get_proper_name(self, model, _id):
        return u'{}_{}'.format(model.replace('.', '_'), _id)

    def _create_dest_mapping(self):
        # Create a translation dict for dest_record_id -> dest_record_data
        self.dest_trans = defaultdict(dict)
        dest_external_ids = self.dest_ir_model_obj.search([
            ('name', '=ilike', self._prefix+'%')
        ])
        dest_external_records = self.dest_ir_model_obj.browse(
            dest_external_ids
        ).read()
        if dest_external_records:
            for r in dest_external_records:
                model = r['model']
                dest_id = r['res_id']
                source_id = int(str(r['name'].split('.')[-1]).split('_')[-1])
                self._add_dest_id(model, source_id, dest_id)

    def _find_dest_id(self, model, source_id):
        return source_id and self.dest_trans.get(model, {}).get(source_id, {})

    def _add_dest_id(self, model, source_id, dest_id):
        self.dest_trans[model][source_id] = dest_id

    def _prepare_sync(self, model, domain=None,
            excluded_fields=None, since=None, context=None):

        # Initialize
        self.source_records = []
        self.source.env.context.update(context or {})
        self.dest.env.context.update(context or {})
        source_obj = self.source.env[model]
        excluded_fields_set = set(excluded_fields or []).union(
            set(DEFAULT_EXCLUDED_FIELDS))
        self._many2one_fields = {}  # dict of many2one fields {name->relation object}

        # Determine which fields to sync exactly
        self._log("Determining which fields to sync...")
        fields = self.source_ir_fields.search([('model', '=', model)])
        fields_info = self.source_ir_fields.browse(fields).read([])
        model_fields = []
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
            if relation and ttype == 'many2one':
                self._many2one_fields[name] = relation
            model_fields.append(name)
        self._fields_struct[model] = model_fields

        # Fetch source records
        if since:
            domain.append(('write_date', '>', since))
        self._log(u'Searching which records satisfy: {}'.format(domain))
        source_obj_ids = source_obj.search(domain or [])
        self._log(u'Reading {} records from source server...'.format(len(source_obj_ids)))
        records = source_obj.browse(source_obj_ids).with_context({
            'mail_create_nosubscribe': True
        }).read(model_fields)

        # Sort records so that parents always come before children
        if records and 'parent_id' in records[0].keys():
            self._log(u'Sorting records according to parent-child hierarchy...')
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
            self.source_records = _sort(records, [])
        else:
            self.source_records = records

    def _get_proper_name(self, model, id):
        return u'{}_{}'.format(model.replace('.', '_'), id)

    def _map_fields(self, model, data):
        vals = data.copy()
        for field, rel_model in self._many2one_fields.iteritems():
            source_id = data.get(field) and data.get(field)[0]
            if source_id:
                self._log("Translating {}[{}]...".format(rel_model, source_id), cr=False)
                dest_id = self._find_dest_id(rel_model, source_id)
                if dest_id:
                    self._log(str(dest_id))
                    vals[field] = dest_id
                else:
                    dest_id = self.manual_mapping.get(rel_model, {}).get(source_id)
                    self._log(str(dest_id))
                    if dest_id:
                        vals[field] = dest_id
                    else:
                        self._log('Mapping failed: consider adding manual mapping for record {}[{}]'.format(
                            rel_model, source_id))
                        vals[field] = None
        return vals

    def _write_or_create_model_record(self, model, vals, source_id):
        dest_id = self._find_dest_id(model, source_id)
        if dest_id:
            record = self.dest.env[model].write(dest_id, vals)
            self._log(u'updated record {}[{}] from source {}'.format(model, dest_id, source_id))
        else:
            dest_id = self.dest.env[model].create(vals)
            self._add_dest_id(model, source_id, dest_id)
            self._log(u'created record {}[{}] from source {}'.format(model, dest_id, source_id))
            self.dest_ir_model_obj.create({
                'model': model,
                'name': self._prefix + self._get_proper_name(model, source_id),
                'res_id': dest_id,
            })

    def _sync_one_model(self, model, domain=None,
            excluded_fields=None, since=None, context=None):
        self._log(u'Preparing sync of model {}'.format(model))
        self._prepare_sync(model, domain=domain or [],
            excluded_fields=excluded_fields or [], since=since,
            context=context)
        self._log(u'Writing {} records to target server...'.format(len(self.source_records)))
        for source_record in self.source_records:
            source_id = source_record['id']
            mapped = self._map_fields(model, source_record)
            self._write_or_create_model_record(model, mapped, source_id)

    def sync(self, model, since=None):
        domain = model.get('domain')
        context = model.get('context')
        excluded_fields = model.get('excluded_fields')
        model_to_sync = model.get('model')
        self._sync_one_model(
            model_to_sync,
            domain=domain,
            excluded_fields=excluded_fields,
            since=since,
            context=context
        )

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
 
