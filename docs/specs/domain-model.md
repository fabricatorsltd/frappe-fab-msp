# Domain Model

## DocTypes

### Customer Service Type

Catalog of managed-service kinds.

- type name (unique)
- category: License / Hardware / Cloud / Network / Other
- default billing item
- default billing mode: None / Recurring / One-time

### Customer Service

A configuration item: one managed service for one customer. Naming `CS-.#####`.

- customer, service type
- service label, identifier (UPN / serial / URL)
- status: Active / Suspended / Terminated
- quantity, start date, renewal date (co-termination date of the SKU pool)
- billing item, billing mode, billing interval (Monthly / Annual), subscription

On validate, billing item and mode are inherited from the service type when
blank. The SKU pool for co-termination is the Active Customer Service with the
same customer and billing item.

### MSP Billing Charge

A deferred charge for a consolidated customer, recorded by fulfillment instead of
an immediate invoice. Naming `MSP-CHG-.#####`.

- customer, posting date, status (Unbilled / Billed)
- item, qty, rate, description
- hd_ticket, customer_service, sales_invoice (set when consolidated)

The month-end run (`fab_msp.billing`) rolls Unbilled charges plus the active
recurring pools into one draft invoice per customer.

## Custom fields

### HD Ticket Type (service catalog)

- `fab_requires_approval`, `fab_approval_by` (Internal Manager / Customer Manager)
- `fab_cmdb_effect` (None / Create / Modify), `fab_service_type`
- `fab_billable`, `fab_billing_item`, `fab_billing_mode`, `fab_coterm_prorate`

### HD Ticket (request instance)

- `fab_customer_service` (the service acted on)
- `fab_approval_status` (Not Required / Awaiting Service / Pending / Approved / Rejected)
- `fab_billing_status` (Not Billable / Pending / Invoiced / Subscribed)
- `fab_sales_invoice`, `fab_subscription`

All hidden from the customer portal; surfaced to agents via the Default ticket
template.

## Form scripts (HD Form Script)

- `MSP Ticket Actions` (agent): Create service + Approve/Reject.
- `MSP Ticket Approval (Portal)` (customer portal): Approve/Reject.

Both gate Approve/Reject on `fab_msp.api.can_approve_ticket`.

## Server surface

- `fab_msp.ticket.apply_service_rules` (HD Ticket validate): derives approval and
  billing status from the type.
- `fab_msp.api`: `create_service_from_ticket`, `request_approval`,
  `can_approve_ticket`, `set_ticket_approval`.
- `fab_msp.fulfillment.fulfill_ticket`: provisioning + billing on approval, run
  as a system operation.

## Notifications and i18n

- `request_approval` emails the approver(s) using the editable Email Template
  `MSP Approval Requested`, rendered per recipient in their own language.
- All UI strings, toasts and messages go through the translation system. Source
  is English (`msgid`); `locale/it.po` and `locale/fr.po` provide Italian and
  French. Run `bench compile-po-to-mo --app fab_msp` after editing.

## External model reused

- Visibility and customer managers: Helpdesk `HD Customer` / `HD Customer Member`
  (`is_manager`) and its native ticket permission query. Not forked.
- Billing: ERPNext Customer price list, Tax Category / Sales Taxes template,
  Sales Invoice, Subscription / Subscription Plan.
