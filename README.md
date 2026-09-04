## Fab MSP

Managed-services layer on top of ERPNext and Frappe Helpdesk: a registry of the
services a customer runs, a request catalog on ticket types, approval routing,
and the billing/CMDB glue that fires when a request is approved.

Graph is only the transport for inbound mail (see `fab_graph_mail`); this app is
the service desk and its ERP connection.

### What it adds

- **Customer Service** (CMDB): a configuration item linking a customer to a
  managed service (M365 mailbox/license, Adobe, hardware, network, SharePoint
  site). Carries quantity, term/renewal date (co-termination), status and the
  billing item. **Customer Service Type** is the catalog of types with default
  billing item and mode.
- **Service catalog on HD Ticket Type**: per request type, whether it needs
  approval and by whom, its CMDB effect (create/modify/none), and whether it is
  billable (item, mode, co-terminate & pro-rate).
- **Ticket fields and actions**: the MSP fields show in the agent view; a form
  script adds "Create service" (agent) and "Approve/Reject" (agent and customer
  portal), each visible only to whoever may act.
- **Approval routing**: `Customer Manager` requests are decided by a manager of
  the ticket's customer (helpdesk `is_manager`), not our staff; `Internal
  Manager` by our Agent/System managers.
- **Billing fulfillment**: on approval of a billable request the app provisions
  the service and generates ERP artefacts, deriving price from the customer's
  price list and tax from the customer's tax category.
- **Field service**: an on-site intervention is an ERPNext Task carrying the
  TRISON "parte de ticket", printed back on the same form. Closing it bills a
  call-out plus the hours (rounded up to the half hour) into the customer's
  consolidated invoice; the *Field Service Settlement* report says which
  technician invoice may be paid because the customer has paid. External
  technicians get desk access to their own jobs only, without the money on them.
- **Signature**: the parte is signed on the spot by the technician and the shop
  manager, each with an OTP, through OpenAPI's European eSignature (SES) on the
  account `fab_openapi` already holds; the signed parte and its audit trail come
  back onto the task as private files.

### Visibility

Ticket visibility uses Helpdesk's native model (no fork): a customer manager
(`is_manager` on the HD Customer member) sees the customer's tickets, a plain
member sees their own, segregated per customer.

### Documentation

- `docs/operator-guide.md` -- how to configure and run it.
- `docs/specs/domain-model.md` -- doctypes, fields, relationships.
- `docs/specs/workflows.md` -- request lifecycle from ticket to billing.

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/fabricatorsltd/frappe-fab-msp.git --branch version-16
bench --site [site] install-app fab_msp
# a running dev server must be restarted so the new app is importable
```

### License

agpl-3.0
