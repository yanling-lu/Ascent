"""Reasoning Trace Builder.

Assembles the ordered trace — each reasoning step, each named Agent_Tool invoked,
the data retrieved, and the resulting decision — and masks direct customer
identifiers (customer_uid, cnsmr_prof_sid) so individuals cannot be re-identified
(Requirements 7.1, 7.5, 10.3).
"""
