# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import json
import frappe
from frappe import _
from frappe.utils import cint, flt, getdate
from pypika.terms import JSON


def execute(filters=None):
	param_columns = get_params()
	
	if not filters: filters = {}
	if filters.from_date > filters.to_date:
		frappe.throw(_("From Date must be before To Date"))

	float_precision = cint(frappe.db.get_default("float_precision")) or 3

	columns = get_columns(filters, param_columns)
	item_map = get_item_details(filters)
	iwb_map = get_item_warehouse_batch_map(filters, float_precision, param_columns)

	data = []
	for item in sorted(iwb_map):
		if not filters.get("item") or filters.get("item") == item:
			for wh in sorted(iwb_map[item]):
				for batch in sorted(iwb_map[item][wh]):
					batch_dict = iwb_map[item][wh][batch]
					if batch_dict.bal_qty:
						row = [item, item_map[item]["item_name"], wh, batch,
							flt(batch_dict.bal_qty, float_precision),
							item_map[item]["stock_uom"], batch_dict.qi, batch_dict.desc, batch_dict.supplier_bag_no
						]
						for param in param_columns:
							row.append(batch_dict[param['col_name']])
						data.append(row)

	return columns, data


def get_columns(filters, params):
	"""return columns based on filters"""

	columns = [_("Item") + ":Link/Item:100"] + [_("Item Name") + "::150"] + [_("Warehouse") + ":Link/Warehouse:100"] + \
		[_("Batch") + ":Link/Batch:100"] + [_("Balance Qty") + ":Float:90"] + [_("UOM") + "::90"] + \
		[_("Quality Inspection") + ":Link/Quality Inspection:140"] +[_("Description") + "::95"] + [_("Supplier Bag No.") + "::120"]
	
	for param in params:
		columns += [_(param['inspection_parameter']) + ":Float:100"] 
		
	return columns


def get_conditions(filters, params):
	conditions = ""
	if not filters.get("from_date"):
		frappe.throw(_("'From Date' is required"))

	if filters.get("to_date"):
		conditions += " and posting_date <= '%s'" % filters["to_date"]
	else:
		frappe.throw(_("'To Date' is required"))

	for field in ["item_code", "warehouse", "batch_no", "company"]:
		if filters.get(field):
			conditions += " and {0} = {1}".format(field, frappe.db.escape(filters.get(field)))
	
	for param in params:
		if filters.get(param['col_name']):
			parameter = '%' + filters.get(param['col_name']) + '%'
			conditions += " and reading." + param['col_name'] + " like {0}".format(frappe.db.escape(parameter, percent = False))

	if filters.get("supplier_bag_no"):
		supplier_bag_no = '%' + filters.get('supplier_bag_no') + '%'
		conditions += " and se.supplier_bag_no like {0}".format(frappe.db.escape(supplier_bag_no), percent = False)

	return conditions


# get all details
def get_stock_ledger_entries(filters, params):
	conditions = get_conditions(filters, params)
	param_conditions = ""
	for param in params:
		param_conditions += ",MAX(case when specification = {0} then reading_1 end) {1}" \
			.format(frappe.db.escape(param['inspection_parameter'], percent = False), param['col_name'])

	col_conditions = ""
	for col in params:
		col_conditions += ", reading." + col['col_name'] + " as " + col['col_name']
	
	return frappe.db.sql("""
		select s.item_code, s.batch_no, s.warehouse, s.posting_date, sum(s.actual_qty) as actual_qty,
			se.quality_inspection as qi_name, se.supplier_bag_no as supplier_bag_no %s
		from `tabStock Ledger Entry` s
		left join `tabStock Entry Detail` se on s.voucher_no = se.parent and se.batch_no= s.batch_no
		left join (
			select parent %s
			from `tabQuality Inspection Reading` group by parent) as reading	
		on se.quality_inspection = reading.parent
		where s.is_cancelled = 0 and s.docstatus < 2 and ifnull(s.batch_no, '') != '' %s
		group by voucher_no, batch_no, s.item_code, warehouse
		order by s.item_code, warehouse""" %
		(col_conditions, param_conditions, conditions), as_dict=1)
	
def get_item_warehouse_batch_map(filters, float_precision, params):
	sle = get_stock_ledger_entries(filters, params)
	iwb_map = {}

	from_date = getdate(filters["from_date"])
	to_date = getdate(filters["to_date"])

	for d in sle:
		param_values = {}
		for param in params:
			param_values[param['col_name']] = ""
		iwb_map.setdefault(d.item_code, {}).setdefault(d.warehouse, {})\
			.setdefault(d.batch_no, frappe._dict({
				"opening_qty": 0.0,
				"in_qty": 0.0,
				"out_qty": 0.0,
				"bal_qty": 0.0,
				"qi": "",
				"desc": get_batch_desc(d.batch_no),
				"supplier_bag_no": ""
			}))
		batch_dict = iwb_map[d.item_code][d.warehouse][d.batch_no]
		batch_dict.qi = d.qi_name
		batch_dict.supplier_bag_no = d.supplier_bag_no
		for param in param_values:
			batch_dict[param] = d[param]
		batch_dict.bal_qty = flt(batch_dict.bal_qty, float_precision) + flt(d.actual_qty, float_precision)

	return iwb_map

@frappe.whitelist()
def get_params():
	params =  frappe.db.get_values('Inspection Report Parameter',
		{'parent': 'Quality Inspection Report Settings'},
		['inspection_parameter'], order_by = 'idx', as_dict = 1)
	for col in params:
		col['col_name'] = col['inspection_parameter'].split()[0].lower()
	return params

def get_batch_desc(batch_no):
	desc = frappe.get_value('Batch', batch_no, 'description')
	return desc
def get_item_details(filters):
	item_map = {}
	for d in frappe.db.sql("select name, item_name, description, stock_uom from tabItem", as_dict=1):
		item_map.setdefault(d.name, d)

	return item_map

@frappe.whitelist()
def create_stock_entry(item_list):
	stock_entry = frappe.new_doc('Stock Entry')
	stock_entry.purpose = 'Material Transfer'

	item_list_obj = json.loads(item_list)
	#remove duplicates
	final_list = [dict(t) for t in {tuple(d.items()) for d in item_list_obj}]

	for item_details in final_list:							
		se_child = stock_entry.append('items')
		se_child.s_warehouse = item_details["warehouse"]
	
		for field in ["item_code","uom","qty","quality_inspection",
			 "item_name", "batch_no"]:
			if item_details.get(field):
				se_child.set(field, item_details.get(field))

		if se_child.s_warehouse==None:
			se_child.s_warehouse = stock_entry.from_warehouse
		if se_child.t_warehouse==None:
			se_child.t_warehouse = stock_entry.to_warehouse

		se_child.transfer_qty = flt(item_details["qty"], se_child.precision("qty"))

	stock_entry.set_stock_entry_type()

	return stock_entry.as_dict()


@frappe.whitelist()
def create_certificate(item_list):
	
	item_list_obj = json.loads(item_list)
	#remove duplicates
	final_list = [dict(t) for t in {tuple(d.items()) for d in item_list_obj}]

	item_code = item_list_obj[0]["item_code"]
	analytical_certificate = frappe.new_doc('Analytical Certificate Creation')
	analytical_certificate.item_code = item_code
	analytical_certificate.item_name = frappe.get_value("Item",item_code,"item_name")
	
	for item_details in final_list:	
		if item_details["item_code"] != item_code:					
			frappe.throw("Please select rows of same Item Code, to create a certificate!")
		drum_child = analytical_certificate.append('batches')
		drum_child.drum = item_details["batch_no"]
		qi_doc = frappe.get_doc("Quality Inspection",item_details["quality_inspection"])
		qi_readings = qi_doc.get("readings")
		for qi in qi_readings:
			mapped_column = frappe.get_value("Quality Inspection Parameter",qi.specification,"certificate_column_name")
			drum_child.set(mapped_column,qi.reading_1)
	return analytical_certificate.as_dict()