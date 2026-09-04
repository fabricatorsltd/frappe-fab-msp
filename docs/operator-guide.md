# Operator Guide

Configuring and running `fab_msp`. For the data model see `docs/specs/`.

## One-time setup

### Service types

Open **Customer Service Type** and create the catalog of managed-service kinds
(M365 License, M365 Mailbox, Adobe License, Computer, SharePoint Site, Network
Device...). Set a default billing item and mode where it applies; a Customer
Service inherits them unless overridden.

### Request catalog (HD Ticket Type)

Each ticket type carries the request rules, section "Service Request Rules":

- **Requires Approval** + **Approval By**: `Customer Manager` (the requester's
  business manager, decided on the customer side) or `Internal Manager` (our
  Agent/System managers).
- **CMDB Effect**: `Create` provisions a new service, `Modify` acts on an
  existing one (the ticket must link a Customer Service before it can be
  approved), `None` for pure support.
- **Billable** + **Billing Item** + **Billing Mode** (One-time / Recurring) +
  **Co-terminate & pro-rate** for adding seats to an existing SKU pool.

### Customer members and managers

Ticket visibility and customer-side approval use the native Helpdesk model. On
each **HD Customer**, list the members (contacts) and tick **is_manager** for the
people who may see the whole customer's tickets and approve `Customer Manager`
requests. A plain member sees only their own tickets; segregation is per
customer; a user with no customer sees only their own.

### Billing prerequisites (per customer)

Fulfillment derives price and tax from the customer, so configure the customer,
not the app:

- **Price**: set the customer's price list (dedicated lists are supported) and
  keep an Item Price for each billing item in that list. The rate read is
  treated as the annual price.
- **Tax**: set the customer's Tax Category and give each Sales Taxes and Charges
  Template the matching `tax_category`. A domestic customer resolves to standard
  VAT; a foreign customer to a 0% template with the right natura.

## Special / foreign customer (recipe)

A foreign customer billed without VAT and on its own price list needs only
instance configuration:

1. Tax Category, e.g. `Estero`, on the customer.
2. A 0% Sales Taxes template (e.g. `IVA 0 N2.1`) with `tax_category = Estero`
   and its **Tax Exemption Reason** (natura) set on the tax row.
3. A dedicated price list on the customer with Item Prices for the billed SKUs.

An added seat then produces a pro-rata invoice at the customer's price, 0% with
the natura, co-terminated to the pool renewal date.

## Day to day

1. A ticket is raised (email, portal, agent) and given a request type.
2. On save the ticket derives its billing status; a request needing approval
   sits at "Awaiting Service" (no manager is notified yet).
3. The agent provisions the service: "Create service" opens the new service form
   to complete its details, or "Modify" links an existing one. "Open service"
   navigates to it.
4. The agent clicks "Request approval": the request goes Pending and the
   approver is emailed. A manager is asked only for a real, completed service.
5. The approver (customer manager, or our staff) uses Approve/Reject; the button
   shows only to who may decide.
6. On approval of a billable request the app provisions/updates the service and
   creates the ERP artefacts:
   - **Co-terminate & pro-rate**: adds the seat to the existing SKU pool and
     issues a pro-rata Sales Invoice to the pool renewal date, plus a
     Subscription for the recurring part.
   - **Recurring** (new pool): a Subscription.
   - **One-time**: a Sales Invoice.
   Invoices are left as **draft** for review before they go to SdI.

## Notifications and languages

The approval-request email uses the **Email Template** `MSP Approval Requested`;
edit it (subject/body, Jinja) to change the wording without code. It is rendered
in each recipient's own language.

The UI ships English (source), Italian and French. To adjust wording, edit
`locale/it.po` / `locale/fr.po` and run `bench compile-po-to-mo --app fab_msp`.

## Quantity and billing triggers

- **Quantity**: the ticket's Quantity field drives how many seats/units the
  request bills and adds to the pool (default 1).
- **When billing fires**: requests needing approval bill on approval; billable
  requests that do not need approval bill when the ticket is Resolved/Closed.
  Fulfillment is idempotent, so it never double-charges.

## Billing mode (immediate vs consolidated)

The customer's **MSP Billing Mode** decides how additions are invoiced:

- **Immediate**: each approved request produces its own Sales Invoice (pro-rata
  co-term, one-time, or a Subscription for a new recurring pool).
- **Consolidated**: additions do not invoice on their own. They provision the
  service and record an `MSP Billing Charge` (status Deferred on the ticket).

Each recurring service has a **Billing Interval** (Monthly or Annual), so one
consolidated invoice can mix frequencies (e.g. Acronis and IT support monthly,
M365 annual with pro-rata). The item price is the rate for that interval (a
monthly item price for monthly services, an annual one for annual services).

For consolidated customers, run the month-end consolidation
(`fab_msp.billing.generate_consolidated_invoices`, or per customer with
`generate_for_customer`). It creates one **draft** invoice per customer with:

- monthly services at their rate, every month;
- annual services at their rate, only in their renewal month (matched by
  month-of-year, so they recur each anniversary);
- the period's deferred additions (annual pro-rata to the co-term date; monthly
  seats need no separate line, they join the base next month).

Newly added seats are excluded from the same-period base so nothing is billed
twice. Review and submit the draft manually.

Run it from the desk: **MSP > Consolidated Billing**, set the posting date and
click *Generate Consolidated Invoices*; the created drafts are listed.

## Field service (TRISON parte de ticket)

An on-site intervention is an ERPNext **Task** under the project **TRISON field
service**, opened from **MSP > Field Service**. It carries the Trison form: parte
number, ticket id, end customer and address, ticket description, work performed,
who went (an internal Employee or an external Supplier), arrival and departure
time, travel time, material used/removed, travel rows, and the pre-filled parte
received from Trison as an attachment. Attachments on a task are forced private.

1. Open a task, fill the parte fields and attach the Trison PDF.
2. **Hours Billed** defaults to the time on site rounded **up to the next half
   hour** (10:00 to 11:10 bills 1.5 h; a departure earlier than the arrival is a
   job past midnight). Travel is paid by the call-out fee, so it is not in the
   hours. The field is editable and a manual value survives later edits of the
   times.
3. Set the status to **Completed**. That bills the intervention: two
   `MSP Billing Charge` rows for the project's customer, a call-out (fee, default
   40) and the hours (hourly rate, default 40), described with parte, date and
   end customer, in the customer's language. Completing twice never bills twice,
   and a project with no customer refuses the close.
4. Correcting the hours or a fee afterwards updates the charges as long as they
   are still Unbilled; once they are on a submitted invoice they are frozen.
5. Print **Parte de Ticket TRISON** to send Trison the completed form (one A4
   page, rendered by Chrome).

The charges join the customer's next consolidated invoice. The task shows Billed
when that invoice is **submitted**; cancelling it, or deleting the draft, puts the
charges back to Unbilled and the task back to "To bill".

### Signature (electronic parte)

The parte can be signed on the spot instead of on paper. Both signatures of the
form are collected in one request: **FIRMA INSTALADOR** by the technician and
**FIRMA ENCARGADO Y SELLO DE TIENDA** by the shop manager, each with an OTP.

Set it up once in **MSP > Signature Settings** (System Manager only):

- **Enabled**, **Environment** (Sandbox while testing, Production after).
- **Account Email** and **API Key** of the OpenAPI account: they are the
  credentials of the OAuth token call, nothing else is needed.
- **Callback Secret**: any long random string. It travels with each request and
  is checked when the provider calls back; without it no callback is accepted.
- **Notify Email**: where the signed parte and its audit trail are mailed. Empty
  means no mail, the files still land on the task.
- **OTP Channel** (SMS or Email), **Signer Language**, **Sender Name** (opens the
  OTP message), **Days Validity** (a request unsigned after that many days is
  called Expired).

Day to day, on a **Completed** intervention:

1. Fill **Shop Manager**, **Shop Manager Phone** (international format,
   +34600111222) and **Shop Manager Email**. First name and surname are both
   needed: they are what gets printed on the signature.
2. Press **Send for signature**. The app renders the parte, opens the request and
   shows both signing links, the manager's one also as a QR code to hold out so
   they sign on their own phone. **Signing links** brings the same dialog back.
3. The technician signs their own link; the manager signs theirs with the OTP
   they receive. **Signature Status** follows: Sent, then Signed once both are
   through. It shows **Partially signed** in between only if the provider reports
   the state of each signer, which its documented status payload does not carry.
4. When both have signed, the signed parte and the audit trail are attached to
   the task (private), **Signed On** is filled and the mail goes to the notify
   address. The provider calls back on its own; an hourly job also refreshes any
   request left pending, and **Refresh signature status** does it by hand.

The technician's own contact comes from their Supplier's contact, their Employee
record or their user, so only the manager has to be typed in.

### External technicians

`fab_msp.api.setup_field_technician(user, supplier)` (System Manager only) gives a
technician the **Field Technician** role and a User Permission on their Supplier.
They see only their own interventions, in the list and by direct URL, and the
money on the task (call-out fee, hourly rate, billing status, the charges and the
invoices) sits at permission level 1, which only Accounts Manager, Projects
Manager and Projects User hold. Known gap: the global File list still shows the
names of attachments belonging to other suppliers' partes.

### Pay when paid

**MSP > Field Service Settlement** lists the completed interventions with what
was billed to Trison (the sum of the charges, so a later correction of the task
cannot disagree with the invoice), the sales invoice and its status, the
technician's own invoice (field **Technician Invoice** on the task, filled when it
arrives) and its outstanding amount. **Payable now** means Trison's invoice is
Paid and the technician's is not settled; a consolidated invoice only partly paid
counts as unpaid.

## Invoice lifecycle and SdI

MSP invoices (immediate or consolidated) are created as **draft** and already
carry a payment mode (Wire Transfer / MP05) so they pass e-invoicing validation.
The operator reviews the draft and **submits** it; on submit the originating
tickets move to Invoiced. Sending to SdI is the standard **Send to SdI** action
that fab_italy_edi shows on any submitted Sales Invoice, so no MSP-specific step
is needed. Adjust the payment mode on the draft before submit if the customer
pays another way.

## Known configuration gaps

- The sales tax template for a customer with no tax category falls back to the
  company default, then the standard rate. Set the customer's tax category for
  correct natura handling (`fab_italy_tax` now seeds the exemption reason on the
  natura templates).
