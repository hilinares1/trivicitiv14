# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
import logging
import json
from datetime import datetime
from dateutil import parser
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare, float_is_zero, float_repr, float_round


class StockPicking(models.Model):
    _inherit = "stock.picking"

    # used for developing purpose to test barcode scan
    custom_barcode_scanned = fields.Char("Barcode Scanned", store=False)
    fifo_inventory_quant_ids = fields.Many2many('stock.quant', compute='compute_fifo_inventory_quant')

    @api.depends('move_ids_without_package', 'move_ids_without_package.product_id')
    def compute_fifo_inventory_quant(self):
        for record in self:
            product_ids = record.move_ids_without_package.mapped('product_id').filtered(lambda p: p.tracking != 'none')
            quants = self.env['stock.quant']
            if product_ids and record.state not in ['draft', 'cancel', 'done'] and record.picking_type_code == 'outgoing':
                location_id = record.location_id
                for product_id in product_ids:
                    quants1 = self.env['stock.quant']._gather(product_id, location_id)
                    quant_obj = self.env['stock.quant']
                    for quant in quants1:
                        if quant.available_quantity:
                            quant_obj |= quant
                            if len(quant_obj) == 5:
                                quants |= quant_obj
                                break
                    if len(quant_obj) < 5:
                        quants |= quant_obj
            record.fifo_inventory_quant_ids = quants

    @api.onchange('custom_barcode_scanned')
    def _on_custom_barcode_changed(self):
        barcode = self.custom_barcode_scanned
        if barcode:
            self.custom_barcode_scanned = ""
            return self.on_barcode_scanned(barcode)

    def on_barcode_scanned(self, barcode):
        if barcode:
            product_ids = self.move_ids_without_package.mapped('product_id')
            stock_quant = self.env['stock.quant'].search([('lot_id.name', '=', barcode), ('location_id', '=', self.location_id.id), ('product_id', 'in', product_ids.ids)])
            if stock_quant:
                if len(stock_quant) == 1 and stock_quant.available_quantity:
                    product_id = stock_quant[0].product_id
                    move_obj = self.move_ids_without_package.filtered(lambda m: m.product_id == product_id)
                    if move_obj:
                        # if product_id.tracking == 'serial':
                            for move in move_obj:
                                if move.product_uom_qty > move.reserved_availability:
                                    need = move.product_uom_qty - move.reserved_availability
                                    self._update_reserved_quantity(move, need, stock_quant, self.location_id, stock_quant.lot_id)
                                    continue
                else:
                    pass
            else:
                raise ValueError(_('%s NFC Tag not exist for delivered product at %s location' % (barcode, self.location_id.name)))

    def _update_reserved_quantity(self, move, need, stock_quant, location_id, lot_id=None, package_id=None, owner_id=None, strict=True):
        assigned_moves = self.env['stock.move']
        partially_available_moves = self.env['stock.move']
        available_quantity = move._get_available_quantity(location_id, lot_id=lot_id, package_id=package_id,
                                                          owner_id=owner_id, strict=True)

        if not lot_id:
            lot_id = self.env['stock.production.lot']
        if not package_id:
            package_id = self.env['stock.quant.package']
        if not owner_id:
            owner_id = self.env['res.partner']

        taken_quantity = min(available_quantity, need)
        if not strict:
            taken_quantity_move_uom = move.product_id.uom_id._compute_quantity(taken_quantity, move.product_uom,
                                                                               rounding_method='DOWN')
            taken_quantity = move.product_uom._compute_quantity(taken_quantity_move_uom, move.product_id.uom_id,
                                                                rounding_method='HALF-UP')

        quants = []

        if move.product_id.tracking == 'serial':
            rounding = self.env['decimal.precision'].precision_get('Product Unit of Measure')
            if float_compare(taken_quantity, int(taken_quantity), precision_digits=rounding) != 0:
                taken_quantity = 0

        try:
            with self.env.cr.savepoint():
                if not float_is_zero(taken_quantity, precision_rounding=move.product_id.uom_id.rounding):
                    quants = self.env['stock.quant']._update_reserved_quantity(
                        move.product_id, location_id, taken_quantity, lot_id=lot_id,
                        package_id=package_id, owner_id=owner_id, strict=strict
                    )
        except UserError:
            taken_quantity = 0

        # Find a candidate move line to update or create a new one.
        for reserved_quant, quantity in quants:
            to_update = move.move_line_ids.filtered(lambda ml: ml._reservation_is_updatable(quantity, reserved_quant))
            if to_update:
                to_update[0].with_context(
                    bypass_reservation_update=True).product_uom_qty += move.product_id.uom_id._compute_quantity(
                    quantity, to_update[0].product_uom_id, rounding_method='HALF-UP')
            else:
                if move.product_id.tracking == 'serial':
                    for i in range(0, int(quantity)):
                        move_line_vals = move._prepare_move_line_vals(quantity=1, reserved_quant=reserved_quant)
                        move_line_vals.update({'temp_move_id': move._origin.id})
                        move_line = self.env['stock.move.line'].create(move_line_vals)
                        self._compute_move_reserved_availability(move)
                else:
                    move_line_vals = move._prepare_move_line_vals(quantity=quantity, reserved_quant=reserved_quant)
                    move_line_vals.update({'temp_move_id': move._origin.id})
                    self.env['stock.move.line'].create(move_line_vals)
                    self._compute_move_reserved_availability(move)
        if taken_quantity == 0:
            pass
        elif move.product_id.tracking == 'serial':
            if float_compare(need, taken_quantity, precision_rounding=rounding) == 0:
                assigned_moves |= move
            else:
                partially_available_moves |= move
        elif move.product_id.tracking == 'lot':
            if (need - taken_quantity) == 0:
                assigned_moves |= move
            else:
                partially_available_moves |= move
        partially_available_moves.write({'state': 'partially_available'})
        assigned_moves.write({'state': 'assigned'})

    def _compute_move_reserved_availability(self, move_obj):
        """ Fill the `availability` field on a stock move, which is the actual reserved quantity
        and is represented by the aggregated `product_qty` on the linked move lines. If the move
        is force assigned, the value will be 0.
        """
        if not any(move_obj._ids):
            # onchange
            for move in move_obj:
                move_line_ids = self.move_line_ids_without_package.filtered(lambda m: m.temp_move_id.id == move._origin.id)
                reserved_availability = sum(move_line_ids.mapped('product_qty'))
                move.reserved_availability = move.product_id.uom_id._compute_quantity(
                    reserved_availability, move.product_uom, rounding_method='HALF-UP')
        else:
            # compute
            result = {data['temp_move_id'][0]: data['product_qty'] for data in
                      self.env['stock.move.line'].read_group([('temp_move_id', 'in', [move_obj._origin.id])], ['temp_move_id', 'product_qty'], ['temp_move_id'])}
            for move in move_obj:
                move.reserved_availability = move.product_id.uom_id._compute_quantity(
                    result.get(move._origin.id, 0.0), move.product_uom, rounding_method='HALF-UP')

    def write(self, vals):
        res = super(StockPicking, self).write(vals)
        if len(self) == 1 and self.picking_type_code == 'outgoing' and vals.get('move_line_ids_without_package'):
            is_bool = False
            for line in self.move_line_ids_without_package:
                if line.temp_move_id and not line.move_id:
                    line.move_id = line.temp_move_id.id
                    is_bool = True
            if is_bool:
                self._check_entire_pack()
                # if self.mapped('move_ids_without_package'):
                #     move_lines = self.env['stock.move.line'].search([('temp_move_id', 'in', self.mapped('move_ids_without_package').ids)])
                #     print(move_lines)
                #     for m_line in move_lines.filtered(lambda m: not m.picking_id and not m.move_id and not m.reference):
                #         print(m_line)
                #         try:
                #             m_line.unlink()
                #         except:
                #             pass
        return res


class StockMoveLine(models.Model):
    _inherit = 'stock.move.line'

    temp_move_id = fields.Many2one('stock.move', string='Temp Move')
