# Workflows

## Request lifecycle

1. **Intake.** A ticket is created (inbound email, customer portal, agent) and
   assigned a request type.
2. **Classification.** On validate, `apply_service_rules` sets the billing status
   and, for a request that needs approval, holds it at "Awaiting Service".
   Approval is never auto-set to Pending; in-flight states are never downgraded.
3. **Service provisioning.** For `Create` the agent provisions a Customer Service
   from the ticket ("Create service"), which opens the service form to complete
   its details; for `Modify` the agent links the existing one. "Open service"
   navigates to it.
4. **Approval request.** The agent explicitly requests approval ("Request
   approval"): the status moves to Pending and the approver is notified by email
   (the editable `MSP Approval Requested` template, rendered in the recipient's
   language). A manager is thus asked only for a real, completed service.
5. **Approval.** The approver decides via Approve/Reject. Routing:
   - `Customer Manager`: only a manager (`is_manager`) of the ticket's customer,
     from the customer portal.
   - `Internal Manager`: our Agent/System managers, from the agent view.
   The button is shown only to who may decide (`can_approve_ticket`). A `Modify`
   request cannot be approved without a linked service.
6. **Fulfillment** (on approval of a billable request, run as system):
   - resolve the SKU pool (linked service, existing co-term pool, or a new one)
   - price from the customer's price list, tax from the customer's tax category
   - **co-terminate & pro-rate**: add the seat to the pool, issue a pro-rata
     Sales Invoice to the pool renewal date, ensure a Subscription
   - **recurring** (new pool): ensure a Subscription
   - **one-time**: a Sales Invoice
   - update pool quantity, link invoice/subscription on the ticket, set billing
     status. Invoices stay as draft for review before SdI.

## Idempotency

Fulfillment is a no-op once the ticket has an invoice or subscription linked, so
a re-approval or replay does not double-charge or double-increment the pool.
