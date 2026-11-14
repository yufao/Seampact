"""Independently authored synthetic office reading tasks."""
TASKS = [
('purchase', 'What is the last date to submit the purchase request for the replacement scanner?', ['Purchasing calendar\nReplacement scanner: requests close on 18 March. Quotes received later move to the next review.', 'Facilities note\nThe old scanner is still usable until the replacement is installed. Do not confuse the request deadline with the delivery date of 7 April.'], [['18 march', 'march 18', '18/3', '3/18']]),
('travel', 'How much of the train fare can the employee claim after the company credit is applied? Give the amount in dollars.', ['Travel receipt\nTrain fare: $186. Employer travel credit applied: $46. The remaining amount was paid personally.', 'Expense guide\nClaim only the employee-paid portion of an eligible fare. Do not claim the employer credit again.'], [['140']]),
('room', 'Which room satisfies all the workshop requirements: at least 18 seats, a video screen, and step-free access?', ['Room inventory\nAlder: 24 seats, screen, stairs only. Birch: 20 seats, screen, step-free. Cedar: 16 seats, screen, step-free.'], [['birch']]),
('access', 'Who is the approver for a temporary visitor badge request?', ['Visitor access procedure\nThe host submits a request. Security checks identity and issues the badge. The facilities manager approves the request before issuance.'], [['facilities manager']]),
('meeting', 'At what time does the revised design review begin? Include the time zone.', ['Design review notice\nOriginally scheduled for 10:00 UTC. Updated invitation: 14:30 UTC on Thursday. The earlier invitation is cancelled.'], [['14:30', '2:30 pm', '2.30 pm'], ['utc']]),
('inventory', 'How many working headsets remain available after the reserved and faulty units are excluded?', ['Equipment count\nThere are 52 headsets in the cabinet. Twelve are reserved for onboarding. Five others are faulty and unavailable.', 'Booking policy\nA reserved item is unavailable for general booking, even if it works. Faulty items are kept separately.'], [['35']]),
('incident', 'Which team owns the next update to staff about the printer outage?', ['Incident log\nThe printer vendor is investigating the fault. Workplace support coordinates repair. Internal communications will publish the next staff update at noon.'], [['internal communications']]),
('retention', 'When may the signed supplier form be securely destroyed? Give the month and year.', ['Records schedule\nSigned supplier forms are kept through the third anniversary of the contract end date and may be securely destroyed on the following day.', 'Contract register\nThe Elm contract ended on 30 June 2024. Its signed supplier form is subject to the standard schedule.'], [['july 2027', '2027-07', '07/2027']]),
('onboarding', 'Which action must be completed before the new starter can receive the laptop?', ['Onboarding checklist\nIT prepares the laptop after the account is requested. The starter must complete identity verification before the laptop is handed over. Account orientation follows handover.'], [['identity verification', 'verify identity', 'identity verified']]),
('invoice', 'What is the remaining balance on invoice 731 after both recorded payments? Give the amount in dollars.', ['Invoice 731\nTotal due: $920. No tax adjustment applies.', 'Payment ledger\nPayment A: $300. Payment B: $220. Both are posted against invoice 731.'], [['400']]),
('leave', 'How many leave days will remain after the approved request?', ['Leave account\nOpening balance is 16 days. The approved request uses 6 days. No other changes have been posted.'], [['10']]),
('handover', 'Who takes over the customer handover while Mara is away?', ['Coverage note\nMara is away Monday through Wednesday. Noel owns the customer handover during that period. Priya handles billing questions; she is not the handover owner.'], [['noel']]),
('training', 'Which course must staff finish before operating the new lift?', ['Safety training matrix\nForklift awareness is recommended for warehouse visitors. Lift operation certification is required before operating the new lift. First-aid training is required only for designated responders.'], [['lift operation certification']]),
('schedule', 'On which day is the software maintenance window actually scheduled?', ['Service announcement\nThe preliminary plan mentioned Tuesday. The approved maintenance window is Friday, 22 September, 20:00–22:00 UTC. Tuesday remains a normal service day.'], [['friday']]),
('shipping', 'Which parcel should be sent using the chilled service?', ['Dispatch instructions\nParcel A contains printed manuals and can travel at room temperature. Parcel B contains temperature-sensitive samples and must remain between 2 and 8 degrees C. Parcel C contains cables.', 'Carrier guide\nUse chilled service for items requiring a 2–8 degrees C range; standard service does not guarantee a temperature range.'], [['parcel b', 'parcel: b']]),
('budget', 'What amount remains in the team event budget after the deposit and catering commitment? Give the amount in dollars.', ['Event budget\nApproved budget: $2,400. Venue deposit committed: $700.', 'Event purchase log\nCatering committed: $950. The venue deposit is already included in the budget, not an additional grant.'], [['750']]),
('version', 'Which document version should staff use for the approved handbook?', ['Document register\nVersion 2.1 was a draft. Version 2.2 was approved on 8 May. Version 2.3 is a proposed revision awaiting review.'], [['2.2']]),
('support', 'Which support queue should receive a request to reset a locked employee account?', ['Service routing guide\nHardware damage goes to Device Support. Account lockouts and password resets go to Identity Support. Network outages go to Connectivity Support.'], [['identity support']]),
]
TASKS += [
('archive', 'Which box should be retrieved for the 2022 signed contracts?', ['Archive index\nBox 14: unsigned drafts from 2022. Box 27: signed contracts from 2022. Box 31: signed contracts from 2023.'], [['box 27']]),
('subscription', 'What is the annual cost after the agreed discount? Give the amount in dollars.', ['Subscription quote\nAnnual list price is $1,200. The negotiated annual discount is $180. The discount applies to the full year.'], [['1020', '1,020']]),
('survey', 'How many respondents selected remote attendance?', ['Event survey\nThere were 80 responses. Fifty selected in-person attendance, 24 selected remote attendance and six were undecided.'], [['24']]),
('delivery', 'What is the correct loading dock for the furniture delivery?', ['Delivery arrangements\nSmall parcels use Dock 1. The furniture delivery on Wednesday must use Dock 3 because the large-item lift is there. Food deliveries use Dock 2.'], [['dock 3']]),
('catering', 'Which menu is suitable for a guest who cannot eat dairy?', ['Catering choices\nMenu A contains cheese. Menu B uses oat milk and contains no dairy ingredients. Menu C contains butter.'], [['menu b']]),
('project', 'Which milestone must be completed before the pilot can start?', ['Project plan\nThe pilot starts only after the data migration is signed off. The retrospective occurs after the pilot, and the launch announcement is scheduled later.', 'Migration review\nThe team has completed the rehearsal but the migration sign-off is still pending.'], [['migration', 'migration sign-off', 'data migration']]),
]
# A longer, structured reference is deliberately relevant to one reading task.
TASKS[17][2][0] += ('\n\nRouting details\nDevice Support handles broken screens, damaged chargers and requests for replacement accessories. '
                    'A device that will not power on should be described with its asset tag and the observed symptoms. '
                    'Connectivity Support handles office Wi-Fi, wired network outages and remote connection faults. '
                    'If several people cannot connect, include the location and approximate start time. '
                    'Identity Support handles login failures, locked accounts and password resets. '
                    'Never include a password in the request. If a user cannot access the portal, a manager may submit the request on their behalf. '
                    'Service desk triage can redirect requests that arrive in the wrong queue, but the requester should select the appropriate queue initially. '
                    'The queues do not change the priority rules: the service desk assesses urgency using the number of affected people and the business impact.')

TASKS[17][2][0] += (
    "\n\nRequest preparation\nThe requester should provide a contact method and describe the activity that failed. "
    "For a login issue, provide the application name and the approximate time of the failed attempt. "
    "For a damaged device, include the asset tag and whether the device can be used safely. "
    "For a network issue, include the building, floor and network name where known. "
    "Screenshots can be helpful if they do not expose credentials or other sensitive information. "
    "Do not attach a password, recovery code, access token or private key to a ticket. "
    "The service desk can ask for more information through the ticket if the original report is incomplete. "
    "\n\nPriority and escalation\nPriority is assessed separately from queue routing. "
    "A single user unable to access a non-urgent application is normally handled through the standard queue. "
    "An issue affecting an entire office or preventing a time-critical operation should be described as such, "
    "so the service desk can assess escalation. The requester should not send multiple duplicate tickets to increase priority. "
    "If the impact changes, add the new information to the existing ticket. "
    "A designated incident coordinator may publish updates for widespread service interruptions. "
    "\n\nHandoffs and closure\nA specialist may transfer a ticket when investigation identifies a different cause. "
    "For example, an apparent account problem can later turn out to be a network interruption. "
    "The receiving team records its findings on the same ticket, so the requester does not need to restart the report. "
    "Before closing, support records the resolution and confirms any follow-up the requester needs to complete. "
    "If the same symptoms return after closure, the requester should mention the earlier ticket when opening a new one."
)
