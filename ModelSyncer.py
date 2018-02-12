from pprint import pprint

class ModelSyncer():

    def __init__(self, source, dest):
        self.source = source
        self.dest = dest
        self.source_ir_fields = source.env['ir.model.fields']
        self.dest_ir_model_obj = dest.env['ir.model.data']
        self.dest_trans = {}
        self._fields_struct = {}

    def _prepare_sync(self, model, domain, excluded_fields):
        self.source_trans = {}
        relational_fields = self.source_ir_fields.search([('model', '=', model)])
        relational_fields_name = self.source_ir_fields.browse(relational_fields).read([])
        model_fields = {field.get('name'): field.get('model') for field in relational_fields_name if
                        field.get('name') not in excluded_fields[0]}
        obj_fields = model_fields.keys()
        self._fields_struct.update(dict({model: obj_fields}))
        source_obj = self.source.env[model]
        if domain[0]:
            source_obj_ids = source_obj.search([domain[0]])
        else:
            source_obj_ids = source_obj.search(domain[0])
        dest_external_ids = self.dest_ir_model_obj.search([('model', '=', model)])
        dest_external_records = self.dest_ir_model_obj.browse(dest_external_ids).read()
        source_records = source_obj.browse(source_obj_ids).read()

        for record in source_records:
            field_data = map(lambda x: record[x], self._fields_struct.get(model))
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
        return model.replace('.', '_') + '_' + str(id)

    def _map_fields(self, model, data):
        vals = dict(zip(self._fields_struct.get(model), data))
        trans = {'customer_asset.brand': 'brand_id',
                 'customer_asset.type': 'type_id',
                 'res.partner': 'partner_id',
                 'project.project': 'project_id',
                 'project.task.type': 'stage_id',
                 'res.users': 'user_id',
                 'mail.mail': 'mail_message_id',
                 'mail.message.subtype': 'subtype_id',
                 'customer_asset.model': 'model_id'}

        if model in ('mail.mail', 'mail.message'):
            trans.update({'res.partner': 'author_id'})
        if model == 'res.partner':
            vals = {'name': vals.get('self')[1]}
            # vals.update({'is_company': True})
            return vals

        if model == 'res.users':
            vals = {'name': vals.get('name'),
                    'login': vals.get('login')}
            return vals

        if model in ('customer-asset.type','customer_asset,brand'):
            return vals

        reversed_trans = {k:v for v, k in trans.iteritems()}

        for key in vals.keys():
            if reversed_trans.get(key):
                vals[reversed_trans.get(key)] = vals.pop(key)
        proper_name = []
        for k, v in vals.iteritems():
            if isinstance(v, (list,)) and len(v) != 0:
                proper_name.append(self._get_proper_name(k, v[0]))

        for name in proper_name:
            dest_id = self.dest_ir_model_obj.search([('name', '=', '__export_sfit__.'+name)])
            if dest_id:
                dest_id = self.dest_ir_model_obj.read(dest_id, ['res_id', 'model'])
                related_model = dest_id[0].get('model')
                dest_id = dest_id[0].get('res_id')
                dest_field = trans.get(related_model)
                vals.update({dest_field: dest_id})

        return vals

    def _sync_one_model(self, model, domain, excluded_fields):
        self._prepare_sync(model, domain, excluded_fields)
        if self.source_trans:
            for id, data in self.source_trans.iteritems():
                if self.dest_trans.get(self._get_proper_name(model, id)):
                    pprint(self._map_fields(model, data))
                    existing_record_id = self.dest_trans.get(self._get_proper_name(model, id))['res_id']
                    dest_existing_record = self.dest.env[model].browse(existing_record_id)
                    dest_existing_record.write(self._map_fields(model, data))

                else:
                    if model == 'res.users':
                        if self._map_fields(model, data).get('login') == 'admin':
                            continue

                    record_id = self.dest.env[model].create(self._map_fields(model, data))

                    external_ids = {
                        'model': model,
                        'name': '__export_sfit__.' + self._get_proper_name(model, id),
                        'res_id': record_id,
                        }
                    self.dest_ir_model_obj.create(external_ids)

    def sync(self, model):
        domain = model.get('domain')
        excluded_fields = model.get('excluded_fields')
        model_to_sync = model.get('model')
        self._sync_one_model(model_to_sync, domain, excluded_fields)
