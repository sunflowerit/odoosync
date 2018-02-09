from pprint import pprint
import collections

class ModelSyncer():

    def __init__(self, source, dest):
        self.source = source
        self.dest = dest
        self.source_ir_fields = source.env['ir.model.fields']
        self.dest_ir_model_obj = dest.env['ir.model.data']
        self.excluded_fields = ('__last_update', 'create_date', 'create_uid', 'write_date', 'write_uid')
        self.odoo_main_models = ('utm.medium','res.partner', 'ir.model', 'crm.team', 'account.journal')
        self._depends_struct = {}
        self._fields_struct = {}
        self._non_related_fields ={}
        self.mapping = {}
        self.dest_trans = {}
        self.unique_model = set()

    # def _get_odoo_main_fields(self, model):
    #
    #     odoo_main_fields = self._non_related_fields.get(model)
    #
    #     source_main_obj = self.source.env[model]
    #     source_main_ids = source_main_obj.search([])
    #     source_main_fields = source_main_obj.browse(source_main_ids).read(odoo_main_fields)
    #
    #     dest_main_obj = self.dest.env[model]
    #     dest_main_ids = dest_main_obj.search([])
    #     print ' dest_main_obj ', dest_main_obj, 'odoo main fields ', odoo_main_fields
    #     dest_main_fields = dest_main_obj.read(dest_main_ids, odoo_main_fields)


    def _get_depends(self, model):
        relational_fields = self.source_ir_fields.search([('model', '=', model)])
        relational_fields_name = self.source_ir_fields.browse(relational_fields).read([])
        relational_fields = {field.get('relation'): field.get('name') for field in relational_fields_name if
                            field.get('name') not in self.excluded_fields and field.get('ttype') == 'many2one'}

        non_relational_fields = [field.get('name') for field in relational_fields_name if
                            field.get('name') not in self.excluded_fields and not field.get('ttype') in ('many2one', 'many2many', 'one2many')]

        self.mapping.update(relational_fields)
        relational_objs = filter(None, relational_fields.keys())
        self._depends_struct.update(dict({model: relational_objs}))
        obj_fields = relational_fields.values()
        self._fields_struct.update(dict({model: obj_fields}))
        self._non_related_fields.update(dict({model: non_relational_fields}))

        for related_obj in relational_objs:
            if related_obj not in self.unique_model:
                self.unique_model.add(related_obj)
                self._get_depends(related_obj)
            # elif related_obj in self.odoo_main_models:
            #     self._get_odoo_main_fields(related_obj)

    def _prepare_sync(self, model):
        self.source_trans = {}

        source_obj = self.source.env[model]
        source_obj_ids = source_obj.search([])
        dest_external_ids = self.dest_ir_model_obj.search([('model', '=', model)])
        dest_external_records = self.dest_ir_model_obj.browse(dest_external_ids).read()
        if self._fields_struct.get(model):
            source_records = source_obj.browse(source_obj_ids).read(self._fields_struct.get(model))
            for record in source_records:
                field_data = map(lambda x: record[x], self._fields_struct.get(model))
                self.source_trans.update({record['id']: field_data})
        elif self._non_related_fields.get(model):
            source_records = source_obj.browse(source_obj_ids).read(self._non_related_fields.get(model))
            for record in source_records:
                field_data = map(lambda x: record[x], self._non_related_fields.get(model))
                self.source_trans.update({record['id']: field_data})
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
        source_ids = [x[0] for x in data if isinstance(x,(list,))]
        d = dict(zip(self._depends_struct.get(submodel), source_ids))
        proper_name = []
        for k, v in d.iteritems():
            proper_name.append(self._get_proper_name(k, v))
        for name in proper_name:
            if self.dest_trans.get(name):
                print ' name ', name
                dest_id = self.dest_ir_model_obj.search([('name', '=', '__export_sfit__.'+name)])
                dest_id = self.dest_ir_model_obj.read(dest_id, ['res_id', 'model'])
                related_model = dest_id[0].get('model')
                dest_id = dest_id[0].get('res_id')
                dest_field = self.mapping.get(related_model)
                print ' dest_field ',dest_field,' dest_id', dest_id
                vals.update({dest_field: dest_id})
        return vals

    def _sync_one_model(self, submodel):
        self._prepare_sync(submodel)
        print 'syncing model ', submodel
        if submodel not in self.odoo_main_models:
            for id, data in self.source_trans.iteritems():
                if self._non_related_fields.get(submodel):
                    vals_non_related_fields = dict(zip(self._non_related_fields.get(submodel), data))
                if self._fields_struct.get(submodel):
                    vals_related_fields = dict(zip(self._fields_struct.get(submodel), data))
                # self._map_fields(submodel, data)
                if self.dest_trans.get(self._get_proper_name(submodel, id)):
                    if self._depends_struct.get(submodel):
                        existing_record_id = self.dest_trans.get(self._get_proper_name(submodel, id))['res_id']
                        dest_existing_record = self.dest.env[submodel].browse(existing_record_id)
                        dest_existing_record.write(self._map_fields(submodel, data))
                    else:
                        existing_record_id = self.dest_trans.get(self._get_proper_name(submodel, id))['res_id']
                        dest_existing_record = self.dest.env[submodel].browse(existing_record_id)
                        dest_existing_record.write(vals_non_related_fields)
                else:
                    if self._depends_struct.get(submodel):
                        record_id = self.dest.env[submodel].create(self._map_fields(submodel, data))
                    else:
                        record_id = self.dest.env[submodel].create(vals_non_related_fields)

                    external_ids = {
                        'model': submodel,
                        'name': '__export_sfit__.' + self._get_proper_name(submodel, id),
                        'res_id': record_id,
                    }
                    self.dest_ir_model_obj.create(external_ids)

    def sync(self, model):
        self._get_depends(model)
        for submodel in reversed(self._depends_struct.get(model)):
            self._sync_one_model(submodel)
        self._sync_one_model(model)
