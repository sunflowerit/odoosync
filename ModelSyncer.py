from collections import OrderedDict, defaultdict
from pprint import pprint

class ModelSyncer():

    def __init__(self, source, dest):
        self.source = source
        self.dest = dest
        self.source_ir_fields = source.env['ir.model.fields']
        self.dest_ir_model_obj = dest.env['ir.model.data']
        self._main_odoo_excluded_models = ('mail.followers','account_analytic_analysis.summary.user','account_analytic_analysis.summary.month','account.fiscal.position','account.account','account.invoice','account.analytic.account','account.analytic.line','product.pricelist','crm.case.section','res.country','product.uom','resource.calendar','res.currency','ir.actions.actions','res.country.state','ir.model','res.company',)
        self._depends_struct = OrderedDict()
        self._fields_struct = {}
        self._many2one_fields = defaultdict(list)
        self.dest_trans = {}

    def _prepare_sync(self, model,domain,excluded_fields):
        source_records = []
        self.source_trans = {}

        source_obj = self.source.env[model]
        source_obj_ids = source_obj.search(domain)
        records = source_obj.browse(source_obj_ids).with_context({'mail_create_nosubscribe':True}).read([])
        [source_records.append(record) for record in records]

        fields = self.source_ir_fields.search([('model', '=', model)])
        fields_info = self.source_ir_fields.browse(fields).read([])
        model_fields = [record for record in source_records[0] if record not in excluded_fields and not record.startswith(('in_group_','sel_groups_'))]# move it to domain
        for field in fields_info:
            if field.get('name') in model_fields:
                if field.get('relation') and field.get('relation') not in self._main_odoo_excluded_models:
                    if field.get('relation') not in excluded_fields:
                        self._many2one_fields[field.get('relation')].append(field.get('name'))
        self._fields_struct.update(dict({model:  model_fields}))
        for record in source_records:
            field_data = map(lambda x: record[x], self._fields_struct.get(model))
            self.source_trans.update({record['id']: field_data})

        dest_external_ids = self.dest_ir_model_obj.search([('model', '=', model)])
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

    def _map_fields(self, submodel, data):
        vals = dict(zip(self._fields_struct.get(submodel), data))
        flat_list = [item for sublist in self._many2one_fields.values() for item in sublist]
        for key in vals.keys():
            if key in flat_list:
                for k,v in self._many2one_fields.iteritems():
                    if key in v:
                        vals[k] = vals.pop(key)
        proper_name = []
        for k, v in vals.iteritems():
            if k in self._many2one_fields.keys() and v:
                proper_name.append(self._get_proper_name(k, v[0]))
        for name in proper_name:
            dest_id = self.dest_ir_model_obj.search([('name', '=', '__export_sfit__.'+name)])
            if dest_id:
                dest_id = self.dest_ir_model_obj.read(dest_id, ['res_id', 'model'])
                related_model = dest_id[0].get('model')
                field_id = dest_id[0].get('res_id')
                dest_field_name = self._many2one_fields.get(related_model)
                vals.update({dest_field_name[0]: field_id})
            else:
                print ' name in prop_name doesn\'t exist', name
        pprint(vals)
        return vals

    def _sync_one_model(self, submodel):
        if self.source_trans:
            print 'syncing model  model ', submodel
            for id, data in self.source_trans.iteritems():
                if self.dest_trans.get(self._get_proper_name(submodel, id)):
                    existing_record_id = self.dest_trans.get(self._get_proper_name(submodel, id))['res_id']
                    dest_existing_record = self.dest.env[submodel].browse(existing_record_id)
                    dest_existing_record.write(self._map_fields(submodel, data))
                    print ' write model ', submodel

                else:
                    record_id = self.dest.env[submodel].create(self._map_fields(submodel, data))
                    external_ids = {
                        'model': submodel,
                        'name': '__export_sfit__.' + self._get_proper_name(submodel, id),
                        'res_id': record_id,
                        }
                    self.dest_ir_model_obj.create(external_ids)
                    print 'create  model ', submodel


    def sync(self, model):
        domain = model.get('domain')
        excluded_fields = model.get('excluded_fields')
        model_to_sync = model.get('model')
        self._prepare_sync(model_to_sync,domain[0],excluded_fields[0])
        self._sync_one_model(model_to_sync)