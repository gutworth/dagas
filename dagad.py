#!/usr/bin/env python3
"""DAGA Server"""

import argparse
import json
import ssl
import sys
import uuid

import requests

from bottle import request, route, run

import daga


class GlobalState:

    def __init__(self, contexts):
        self.contexts = contexts
        self.active_auths = {}
        self.bindings = {}

def _internal_begin_challenge_generation(d):
    state.active_auths[d["auth_id"]] = d["client_data"]
    ac, server = state.contexts[d["client_data"]["uuid"]]
    part = daga.Rand.randrange(daga.Q)
    return {
        "n" : part,
        "sig" : daga.dsa_sign(server.private_key, part),
    }

@route("/internal/begin_challenge_generation", method="POST")
def internal_begin_challenge_generation():
    return _internal_begin_challenge_generation(request.json)

def _internal_finish_challenge_generation(d):
    ac, server = state.contexts[state.active_auths[d["auth_id"]]["uuid"]]
    challenge = 0
    for pub, (part, sig) in zip(ac.server_keys, d["parts"]):
        daga.dsa_verify(pub, part, sig)
        challenge += part
    challenge %= daga.Q
    state.active_auths[d["auth_id"]]["challenge"] = challenge
    return {
        "challenge" : challenge,
        "sig" : daga.dsa_sign(server.private_key, challenge),
    }

@route("/internal/finish_challenge_generation", method="POST")
def internal_finish_challenge_generation():
    return _internal_finish_challenge_generation(request.json)

def _internal_check_challenge_response(d):
    auth_ctx = state.active_auths[d["auth_id"]]
    ac, server = state.contexts[auth_ctx["uuid"]]
    client_proof = daga.ClientProof(d["C"], d["R"])
    auth_ctx["C"] = d["C"]
    auth_ctx["R"] = d["R"]
    msg_chain = daga.VerificationChain(daga.Challenge(auth_ctx["challenge"], auth_ctx["T"]),
                                       auth_ctx["ephemeral_public_key"],
                                       auth_ctx["commitments"],
                                       auth_ctx["initial_linkage_tag"],
                                       client_proof)
    msg_chain.server_proofs = [daga.ServerProof(*x) for x in d["server_proofs"]]
    server.authenticate_client(ac, msg_chain)
    sp = msg_chain.server_proofs[-1]
    return {"proof" : (sp.T, sp.c, sp.r1, sp.r2)}

@route("/internal/check_challenge_response", method="POST")
def internal_check_challenge_response():
    return _internal_check_challenge_response(request.json)

def _internal_bind_linkage_tag(d):
    auth_ctx = state.active_auths[d["auth_id"]]
    ac, server = state.contexts[auth_ctx["uuid"]]
    client_proof = daga.ClientProof(auth_ctx["C"], auth_ctx["R"])
    msg_chain = daga.VerificationChain(daga.Challenge(auth_ctx["challenge"], auth_ctx["T"]),
                                       auth_ctx["ephemeral_public_key"],
                                       auth_ctx["commitments"],
                                       auth_ctx["initial_linkage_tag"],
                                       client_proof)
    msg_chain.server_proofs = [daga.ServerProof(*x) for x in d["server_proofs"]]
    # Verify everyone.
    for i in range(len(ac.server_keys)):
        msg_chain.check_server_proof(ac, i)
    sig = daga.dsa_sign(server.private_key, d["bind"])
    linkage_tag = msg_chain.server_proofs[-1].T
    state.bindings[linkage_tag] = (d["bind"], sig)
    return {
        "tag" : linkage_tag,
        "tag_sig" : daga.dsa_sign(server.private_key, linkage_tag),
        "binding_sig" : sig
    }

@route("/internal/bind_linkage_tag", method="POST")
def internal_bind_linkage_tag():
    return _internal_bind_linkage_tag(request.json)

def internal_call(me, srv, name, data):
    if srv == me.id:
        return globals()["_internal_" + name](data)
    return requests.post("http://localhost:{}/internal/{}".format(12345 + srv, name),
                         headers={"content-type" : "application/json"},
                         data=json.dumps(data)).json()

@route("/request_challenge", method="POST")
def request_challenge():
    client_data = request.json
    ac, server = state.contexts[client_data["uuid"]]
    auth_id = str(uuid.uuid4())
    r = {
        "auth_id" : auth_id,
        "client_data" : client_data,
    }
    challenge_parts = []
    for i in range(len(ac.server_keys)):
        d = internal_call(server, i, "begin_challenge_generation", r)
        challenge_parts.append((d["n"], d["sig"]))
    r = {
        "auth_id" : auth_id,
        "parts" : challenge_parts,
    }
    sigs = []
    for i in range(len(ac.server_keys)):
        d = internal_call(server, i, "finish_challenge_generation", r)
        challenge = d["challenge"]
        sigs.append(d["sig"])
    return {"auth_id" : auth_id, "challenge" : challenge, "sigs" : sigs}

@route("/authenticate", method="POST")
def authenticate():
    client_data = request.json
    auth_ctx = state.active_auths[client_data["auth_id"]]
    ac, server = state.contexts[auth_ctx["uuid"]]
    proofs = []
    r = {
        "auth_id" : client_data["auth_id"],
        "C" : client_data["C"],
        "R" : client_data["R"],
        "server_proofs" : proofs, # Will be mutated.
    }
    for i in range(len(ac.server_keys)):
        d = internal_call(server, i, "check_challenge_response", r)
        proofs.append(d["proof"])
    r = {
        "auth_id" : client_data["auth_id"],
        "bind" : client_data["bind"],
        "server_proofs" : proofs,
    }
    sigs = []
    binding_sigs = []
    for i in range(len(ac.server_keys)):
        d = internal_call(server, i, "bind_linkage_tag", r)
        tag = d["tag"]
        sigs.append(d["tag_sig"])
        binding_sigs.append(d["binding_sig"])
    return {
        "tag" : tag,
        "tag_sigs" : sigs,
        "binding_sigs" : binding_sigs,
    }

def main():
    global state

    p = argparse.ArgumentParser(description="Generate a DAGA auth context")
    p.add_argument("auth_context")
    p.add_argument("private_data")
    opts = p.parse_args()

    with open(opts.auth_context, "r", encoding="utf-8") as fp:
        ac_data = json.load(fp)
        uuid = ac_data["uuid"]
        ac = daga.AuthenticationContext(
            ac_data["client_public_keys"],
            ac_data["server_public_keys"],
            ac_data["server_randomness"],
            ac_data["generators"]
        )

    with open(opts.private_data, "r", encoding="utf-8") as fp:
        priv_data = json.load(fp)
        server = daga.Server(priv_data["n"], priv_data["private_key"], priv_data["secret"])

    state = GlobalState({uuid : (ac, server)})

    run(port=server.id + 12345)


if __name__ == "__main__":
    main()
