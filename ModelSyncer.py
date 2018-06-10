from collections import OrderedDict, defaultdict
from pprint import pprint
import re

class ModelSyncer():

    def __init__(self, source, dest):
        self.source = source
        self.dest = dest
        self.source_ir_fields = source.env['ir.model.fields']
        self.dest_ir_model_obj = dest.env['ir.model.data']
        self._depends_struct = OrderedDict()
        self._fields_struct = {}
        self._many2one_fields = defaultdict(list)
        self._one2many_fields = defaultdict(list)
        self._prefix = '__export_sfit__.'

    def _prepare_sync(self, model,domain,excluded_fields):
        source_records = []
        self.source_trans = {}
        self.dest_trans = {}
        source_obj = self.source.env[model]

        print ' domain is ', [domain]

        source_obj_ids = source_obj.search(domain)

        records = source_obj.browse(source_obj_ids).with_context({'mail_create_nosubscribe':True}).read([])
        [source_records.append(record) for record in records]

        fields = self.source_ir_fields.search([('model', '=', model)])
        fields_info = self.source_ir_fields.browse(fields).read([])
        model_fields = [field.get('name') for field in fields_info if field.get('name') not in excluded_fields]
        for field in fields_info:
            if field.get('relation'):
                if field.get('relation') not in excluded_fields:
                    if field.get('ttype') == 'many2one':
                        self._many2one_fields[field.get('relation')].append(field.get('name'))
                    elif field.get('ttype') in ('one2many', 'many2many'):
                        self._one2many_fields[field.get('relation')].append(field.get('name'))

        self._fields_struct.update(dict({model: model_fields}))
        for record in source_records:
            field_data = map(lambda x: record.get(x), self._fields_struct.get(model))
            self.source_trans.update({record['id']: field_data})

        dest_external_ids = self.dest_ir_model_obj.search([('name', 'ilike', self._prefix+'%')])
        dest_external_records = self.dest_ir_model_obj.browse(dest_external_ids).read()

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
        # TODO  cleaner way
        return model.replace('.', '_')+'_'+str(id)

    def _map_fields(self, model, data):
        vals = dict(zip(self._fields_struct.get(model), data))
        # print ' vals before for model  ', model
        pprint(vals)
        many2one_fields = [item for sublist in self._many2one_fields.values() for item in sublist]
        one2many_fields = [item for sublist in self._one2many_fields.values() for item in sublist]
        proper_name = []
        for field,value in vals.items():
            if value:
                if field in many2one_fields:
                    for k, v in self._many2one_fields.iteritems():
                        if field in v:
                            proper_name.append([field,k, value[0]])

                elif field in one2many_fields:
                    for k,v in self._one2many_fields.iteritems():
                        if field in v:
                            for i in value:
                                proper_name.append([field,k, i])


        for record in proper_name:
            field, model_name, proper_name = record[0], record[1], self._get_proper_name(record[1],record[2])
            dest_id = self.dest_ir_model_obj.search([('name', '=', self._prefix+proper_name)])
            if dest_id:
                dest_id = self.dest_ir_model_obj.read(dest_id, ['res_id', 'model'])
                field_id = dest_id[0].get('res_id')
                if field in many2one_fields:
                    vals.update({field: field_id})
                elif field in one2many_fields:
                    _id = int(proper_name.split('_')[-1])
                    _id_index = vals[field].index(_id)
                    vals[field][_id_index] = field_id
            else:
                print '----------------- consider syncing model ', model_name,' with id:', record[2]
                # self._sync_one_model(model_name)
                # self.create_model_record(model_name, vals, record[2])
                # self._sync_one_model(model_name,domain=[],excluded_fields=['ir.model','mail.alias','mail.alias','ir.model.fields'])
        # print ' vals after for model ', model
        # pprint(vals)
        return vals

    def write_model_record(self, model, vals, id):
        existing_record_id = self.dest_trans.get(self._get_proper_name(model, id))['res_id']
        dest_existing_record = self.dest.env[model].browse(existing_record_id)
        dest_existing_record.write(vals)
        print ' write model ', model

    def create_model_record(self, model, vals, id):
        record_id = self.dest.env[model].create(vals)
        external_ids = {
            'model': model,
            'name': self._prefix + self._get_proper_name(model, id),
            'res_id': record_id,
        }
        print 'create  model ', model
        self.dest_ir_model_obj.create(external_ids)

    def _sync_one_model(self, model,domain=False,excluded_fields=False):
        self._prepare_sync(model,domain,excluded_fields)
        if self.source_trans:
            print 'syncing model  model ', model
            for id, data in self.source_trans.iteritems():
                vals = self._map_fields(model, data)
                if self.dest_trans.get(self._get_proper_name(model, id)):
                    self.write_model_record(model, vals, id)
                else:
                    self.create_model_record(model, vals, id)
                if model == 'res.partner':
                    vals = {'name': vals.get('name'), 'login': vals.get('email') or vals.get('name').split(' ')[0]}
                    if self.dest_trans.get(self._get_proper_name('res.users', id)):
                        if vals.get('login') != 'admin':
                            continue
                        self.write_model_record('res.users', vals, id)
                    else:
                        try:
                            self.create_model_record('res.users', vals, id)
                        except:
                            print 'Error in values: ', vals
                            pass

    def sync(self, model):
        domain = model.get('domain')[0]
        excluded_fields = model.get('excluded_fields')[0]
        model_to_sync = model.get('model')
        self._sync_one_model(model_to_sync,domain,excluded_fields)