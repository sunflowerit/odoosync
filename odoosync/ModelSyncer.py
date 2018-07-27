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

    def __init__(self, source, dest, manual_mapping=None):
        self.manual_mapping = manual_mapping or {}
        self.source = source
        self.dest = dest
        self.source_ir_fields = source.env['ir.model.fields']
        self.dest_ir_model_obj = dest.env['ir.model.data']
        self._depends_struct = OrderedDict()
        self._fields_struct = {}
        self._many2one_fields = defaultdict(list)
        self._prefix = '__export_sfit__.'

    def _prepare_sync(self, model, domain=None,
            excluded_fields=None, since=None, context=None):
        if since:
            domain.append(('write_date', '>', since))
        print u'domain: {}'.format(domain)
        source_records = []
        self.source_trans = {}
        self.dest_trans = {}
        self.source.env.context.update(context or {})
        self.dest.env.context.update(context or {})
        source_obj = self.source.env[model]
        source_obj_ids = source_obj.search(domain or [])
        excluded_fields_set = set(excluded_fields or []).union(
            set(DEFAULT_EXCLUDED_FIELDS))

        records = source_obj.browse(source_obj_ids).with_context({
            'mail_create_nosubscribe': True
        }).read([])

        if records and 'parent_id' in records[0].keys():
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

        fields = self.source_ir_fields.search([('model', '=', model)])
        fields_info = self.source_ir_fields.browse(fields).read([])
        model_fields = []
        for field in fields_info:
            name = field.get('name')
            relation = field.get('relation')
            ttype = field.get('ttype')
            readonly = field.get('readonly')
            if readonly:
                continue
            if name in excluded_fields_set or (relation and ttype != 'many2one'):
                continue
            if relation and ttype == 'many2one':
                self._many2one_fields[relation].append(name)
            model_fields.append(name)

        self._fields_struct.update(dict({model: model_fields}))
        for record in source_records:
            field_data = map(lambda x: record.get(x), self._fields_struct.get(model))
            self.source_trans.update({record['id']: field_data})

        dest_external_ids = self.dest_ir_model_obj.search([
            ('name', '=ilike', self._prefix+'%')
        ])
        dest_external_records = self.dest_ir_model_obj.browse(
            dest_external_ids
        ).read()

        if dest_external_records:
            for r in dest_external_records:
                fields_data = {
                    'name': r['name'],
                    'model': r['model'],
                    'module': r['module'],
                    'res_id': r['res_id'],
                }
                source_id = str(r['name'].split('.')[-1])
                self.dest_trans.update({source_id: fields_data})

    def _get_proper_name(self, model, id):
        return u'{}_{}'.format(model.replace('.', '_'), id)

    def _map_fields(self, model, data):
        vals = dict(zip(self._fields_struct.get(model), data))

        many2one_fields = [
            item
            for sublist in self._many2one_fields.values()
            for item in sublist
        ]
        proper_name = []
        for field, value in vals.items():
            if value and (field in many2one_fields):
                for k, v in self._many2one_fields.iteritems():
                    if field in v:
                        proper_name.append([field,k, value[0]])

        for record in proper_name:
            field, model_name, proper_name =\
                record[0],\
                record[1],\
                self._get_proper_name(record[1], record[2])
            dest_id = self.dest_ir_model_obj.search([
                ('name', '=', self._prefix + proper_name)
            ])
            if dest_id:
                dest_id = self.dest_ir_model_obj.read(
                    dest_id, ['res_id', 'model']
                )
                field_id = dest_id[0].get('res_id')
                if field in many2one_fields:
                    vals.update({field: field_id})
            else:
                mapped_val = self.manual_mapping.get(model_name, {}).get(record[2])
                if mapped_val:
                    vals.update({field: self.manual_mapping.get(record[2])})
                    continue
                else:
                    print 'Mapping failed: consider adding manual mapping for record {}[{}]'.format(model_name, record[2])
        return vals

    def _write_model_record(self, model, vals, id):
        record_id = self.dest_trans.get(self._get_proper_name(model, id))['res_id']
        record = self.dest.env[model].write(record_id, vals)
        print u'updated record {}[{}]'.format(model, record_id)

    def _create_model_record(self, model, vals, id):
        record_id = self.dest.env[model].create(vals)
        external_ids = {
            'model': model,
            'name': self._prefix + self._get_proper_name(model, id),
            'res_id': record_id,
        }
        print u'created record {}[{}]'.format(model, record_id)
        self.dest_ir_model_obj.create(external_ids)

    def _sync_one_model(self, model, domain=None,
            excluded_fields=None, since=None, context=None):
        self._prepare_sync(model, domain=domain or [],
            excluded_fields=excluded_fields or [], since=since,
            context=context)
        if self.source_trans:
            print u'syncing model {}'.format(model)
            for id, data in self.source_trans.iteritems():
                vals = self._map_fields(model, data)
                if self.dest_trans.get(self._get_proper_name(model, id)):
                    self._write_model_record(model, vals, id)
                else:
                    self._create_model_record(model, vals, id)

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
 
