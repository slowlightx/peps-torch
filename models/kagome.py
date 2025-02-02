import torch
import groups.su2 as su2
import groups.su3 as su3
import config as cfg
from ctm.generic.env import ENV
from ctm.generic import corrf
from ctm.generic import rdm_kagome  # modified by Yi, 06/15/21
from math import sqrt
from tn_interface import einsum, mm
from tn_interface import view, permute, contiguous
import itertools
import numpy as np


def _cast_to_real(t):
    return t.real if t.is_complex() else t


class KAGOME():
    def __init__(self, phys_dim=3, j=0.0, k=1.0, h=0.0, global_args=cfg.global_args):
        self.dtype = global_args.torch_dtype
        self.device = global_args.device
        self.phys_dim = phys_dim
        self.j = j  # 2-site permutation coupling constant
        self.k = k  # 3-site ring exchange coupling constant
        self.h = h  # 3-site ring exchange coupling constantNN coupling between A & C in the same unit cell

        # For now, only one-site
        self.obs_ops = self.get_obs_ops()
        self.perm2_tri, self.perm3_l, self.perm3_r, self.h2_tri, self.h3_tri, self.h_tri = self.get_h()


    def get_obs_ops(self):
        obs_ops = dict()
        irrep = su2.SU2(self.phys_dim, dtype=self.dtype, device=self.device)
        obs_ops["sz"] = irrep.SZ()
        obs_ops["sp"] = irrep.SP()
        obs_ops["sm"] = irrep.SM()
        obs_ops["SS"] = irrep.SS()
        irrep_su3_def = su3.SU3_DEFINING(dtype=self.dtype, device=self.device)
        obs_ops["tz"] = irrep_su3_def.TZ()
        obs_ops["tp"] = irrep_su3_def.TP()
        obs_ops["tm"] = irrep_su3_def.TM()
        obs_ops["vp"] = irrep_su3_def.VP()
        obs_ops["vm"] = irrep_su3_def.VM()
        obs_ops["up"] = irrep_su3_def.UP()
        obs_ops["um"] = irrep_su3_def.UM()
        obs_ops["y"] = irrep_su3_def.Y()
        # obs_ops["C1"] = irrep_su3_def.C1()
        # obs_ops["C2"] = irrep_su3_def.C2()

        return obs_ops

    def get_h(self):
        pd = self.phys_dim
        irrep = su2.SU2(pd, dtype=self.dtype, device=self.device)
        # identity operator on two spin-S spins
        idp = torch.eye(pd, dtype=self.dtype, device=self.device)
        idp2x2 = torch.eye(pd ** 2, dtype=self.dtype, device=self.device)
        SS = irrep.SS()
        SS = SS.view(pd ** 2, pd ** 2)
        # perm2 = SS + SS @ SS - idp2x2
        # perm2 = perm2.view(pd, pd, pd, pd).contiguous()
        # Equivalently
        irrep_su3_def = su3.SU3_DEFINING(dtype=self.dtype, device=self.device)
        perm2 = 2 * irrep_su3_def.C1() + idp2x2.view(pd, pd, pd, pd).contiguous() / 3
        perm3_l = torch.einsum('ijal,lkbc->ijkabc', perm2, perm2)
        perm3_r = torch.einsum('ijal,klbc->ikjabc', perm2, perm2)

        perm2_tri = torch.einsum('ijab,kc->ijkabc', perm2, idp)
        perm2_tri += torch.einsum('ikac,jb->ijkabc', perm2, idp)
        perm2_tri += torch.einsum('jkbc,ia->ijkabc', perm2, idp)

        h2_tri = self.j * perm2_tri
        h3_tri = (self.k + self.h * 1j) * perm3_l + (self.k - self.h * 1j) * perm3_r
        h_tri = h2_tri + h3_tri

        return perm2_tri, perm3_l, perm3_r, h2_tri, h3_tri, h_tri

    def energy_1site(self, state, env):
        pd = self.phys_dim
        energy = 0.0
        idp = torch.eye(pd, dtype=self.dtype, device=self.device)
        for coord, site in state.sites.items():
            # intra-cell (down triangle)
            norm = rdm_kagome.trace1x1_dn_kagome(coord, state, env, torch.einsum('ia,jb,kc->ijkabc', idp, idp, idp))
            energy += rdm_kagome.trace1x1_dn_kagome(coord, state, env, self.h_tri) / norm
            # inter-cell (up triangle)
            rdm2x2_ring = rdm_kagome.rdm2x2_kagome(coord, state, env, sites_to_keep_00=('B'), sites_to_keep_10=('C'),
                                                   sites_to_keep_01=(), sites_to_keep_11=('A'))
            energy += torch.einsum('ijlabd,lijdab', rdm2x2_ring, self.h_tri)
        energy_per_site = energy / (len(state.sites.items()) * 3.0)
        energy_per_site = _cast_to_real(energy_per_site)
        return energy_per_site

    def eval_obs(self, state, env):
        pd = self.phys_dim
        chirality = 1j * (self.perm3_l - self.perm3_r)
        idp = torch.eye(pd, dtype=self.dtype, device=self.device)
        obs = dict()
        with torch.no_grad():
            for coord, site in state.sites.items():
                norm = rdm_kagome.trace1x1_dn_kagome(coord, state, env, torch.einsum('ia,jb,kc->ijkabc', idp, idp, idp))
                obs["chirality_dn"] = rdm_kagome.trace1x1_dn_kagome(coord, state, env, chirality) / norm
                obs["chirality_dn"] = _cast_to_real(obs["chirality_dn"])
                obs["avg_bonds_dn"] = rdm_kagome.trace1x1_dn_kagome(coord, state, env, self.perm2_tri) / norm
                obs["avg_bonds_dn"] = _cast_to_real(obs["avg_bonds_dn"]) / 3.0

                rdm2x2_ring = rdm_kagome.rdm2x2_kagome(coord, state, env, sites_to_keep_00=('B'),
                                                       sites_to_keep_10=('C'), sites_to_keep_01=(),
                                                       sites_to_keep_11=('A'))
                obs["chirality_up"] = torch.einsum('ijlabd,lijdab', rdm2x2_ring, chirality)
                obs["chirality_up"] = _cast_to_real(obs["chirality_up"])
                obs["avg_bonds_up"] = torch.einsum('ijlabd,lijdab', rdm2x2_ring, self.perm2_tri)
                obs["avg_bonds_up"] = _cast_to_real(obs["avg_bonds_up"]) / 3.0

        # prepare list with labels and values
        obs_labels = ["avg_bonds_dn", "avg_bonds_up", "chirality_dn", "chirality_up"]
        obs_values = [obs[label] for label in obs_labels]
        return obs_values, obs_labels

    def energy_1triangle(self, state, env):
        energy_dn = 0.0
        energy_up = 0.0
        pd = self.phys_dim
        idp = torch.eye(pd, dtype=self.dtype, device=self.device)
        for coord, site in state.sites.items():
            # intra-cell (down)
            norm = rdm_kagome.trace1x1_dn_kagome(coord, state, env, torch.einsum('ia,jb,kc->ijkabc', idp, idp, idp))
            energy_dn += rdm_kagome.trace1x1_dn_kagome(coord, state, env, self.h_tri) / norm
            # inter-cell (up)
            rdm2x2_up = rdm_kagome.rdm2x2_kagome(coord, state, env, sites_to_keep_00=('B'), sites_to_keep_10=('C'),
                                                 sites_to_keep_01=(), sites_to_keep_11=('A'))
            energy_up += torch.einsum('ijlabd,lijdab', rdm2x2_up, self.h_tri)
        energy_dn = energy_dn / (len(state.sites.items()) * 3.0)
        energy_up = energy_up / (len(state.sites.items()) * 3.0)
        return _cast_to_real(energy_dn), _cast_to_real(energy_up)

    def eval_generators(self, state, env):
        pd = self.phys_dim
        gens = dict({"generators_A": torch.zeros(8, dtype=self.dtype), "generators_B": torch.zeros(8, dtype=self.dtype),
                     "generators_C": torch.zeros(8, dtype=self.dtype)})
        with torch.no_grad():
            for coord, site in state.sites.items():
                for site_type in ['A', 'B', 'C']:
                    rdm1x1 = rdm_kagome.rdm1x1_kagome(coord, state, env, sites_to_keep=(site_type)).view(pd, pd)
                    tmp = dict()
                    for label in ["tz", "tp", "tm", "vp", "vm", "up", "um", "y"]:
                        op = self.obs_ops[label]
                        tmp[f"{label}{coord}"] = einsum('ij,ji', rdm1x1, op)
                    gens[f"generators_{site_type}"] += np.array(
                        [tmp[f"tz{coord}"], tmp[f"tp{coord}"], tmp[f"tm{coord}"],
                         tmp[f"vp{coord}"], tmp[f"vm{coord}"],
                         tmp[f"up{coord}"], tmp[f"um{coord}"], tmp[f"y{coord}"]])
            for site_type in ['A', 'B', 'C']:
                gens[f"generators_{site_type}"] /= len(state.sites.items())
        return gens

    def eval_C2(self, state, env):
        pd = self.phys_dim
        idp = torch.eye(pd, dtype=self.dtype, device=self.device)
        irrep_su3_def = su3.SU3_DEFINING(dtype=self.dtype, device=self.device)
        c2 = irrep_su3_def.C2()
        c2_list = dict({"C2_dn": 0., "C2_up": 0.})
        with torch.no_grad():
            for coord, site in state.sites.items():
                norm = rdm_kagome.trace1x1_dn_kagome(coord, state, env, torch.einsum('ia,jb,kc->ijkabc', idp, idp, idp))
                c2_list["C2_dn"] += rdm_kagome.trace1x1_dn_kagome(coord, state, env, c2) / norm

                rdm2x2_up = rdm_kagome.rdm2x2_kagome(coord, state, env, sites_to_keep_00=('B'), sites_to_keep_10=('C'),
                                                     sites_to_keep_01=(), sites_to_keep_11=('A'))
                c2_list["C2_up"] += torch.einsum('ijlabd,lijdab', rdm2x2_up, c2)

        c2_list["C2_dn"] /= len(state.sites.items())
        c2_list["C2_up"] /= len(state.sites.items())
        return c2_list

    def eval_C1(self, state, env):
        pd = self.phys_dim
        idp = torch.eye(pd, dtype=self.dtype, device=self.device)
        irrep_su3_def = su3.SU3_DEFINING(dtype=self.dtype, device=self.device)
        c1 = irrep_su3_def.C1()
        c1_dict = dict({"C1_AB_dn": 0., "C1_BC_dn": 0., "C1_AC_dn": 0., "C1_AB_up": 0., "C1_BC_up": 0., "C1_AC_up": 0.})
        with torch.no_grad():
            for coord, site in state.sites.items():
                norm = rdm_kagome.trace1x1_dn_kagome(coord, state, env, torch.einsum('ia,jb,kc->ijkabc', idp, idp, idp))
                c1_dict["C1_AB_dn"] += rdm_kagome.trace1x1_dn_kagome(coord, state, env, torch.einsum('ijab,kc->ijkabc', c1, idp)) / norm
                c1_dict["C1_BC_dn"] += rdm_kagome.trace1x1_dn_kagome(coord, state, env, torch.einsum('jkbc,ia->ijkabc', c1, idp)) / norm
                c1_dict["C1_AC_dn"] += rdm_kagome.trace1x1_dn_kagome(coord, state, env, torch.einsum('ikac,jb->ijkabc', c1, idp)) / norm

                rdm2x2_ab = rdm_kagome.rdm2x2_kagome(coord, state, env, sites_to_keep_00=('B'), sites_to_keep_10=(),
                                                     sites_to_keep_01=(), sites_to_keep_11=('A'))
                c1_dict["C1_AB_up"] += torch.einsum('ilad,ilad', rdm2x2_ab, c1)
                rdm1x2_bc = rdm_kagome.rdm2x1_kagome(coord, state, env, sites_to_keep_00=('B'), sites_to_keep_10=('C'))
                c1_dict["C1_BC_up"] += torch.einsum('ijab,ijab', rdm1x2_bc, c1)
                rdm2x1_ac = rdm_kagome.rdm1x2_kagome(coord, state, env, sites_to_keep_00=('C'), sites_to_keep_01=('A'))
                c1_dict["C1_AC_up"] += torch.einsum('ijab,ijab', rdm2x1_ac, c1)

            c1_dict["C1_AB_dn"] /= len(state.sites.items())
            c1_dict["C1_BC_dn"] /= len(state.sites.items())
            c1_dict["C1_AC_dn"] /= len(state.sites.items())
            c1_dict["C1_AB_up"] /= len(state.sites.items())
            c1_dict["C1_BC_up"] /= len(state.sites.items())
            c1_dict["C1_AC_up"] /= len(state.sites.items())
            c1_dict["total_C1_dn"] = c1_dict["C1_AB_dn"] + c1_dict["C1_BC_dn"] + c1_dict["C1_AC_dn"]
            c1_dict["total_C1_up"] = c1_dict["C1_AB_up"] + c1_dict["C1_BC_up"] + c1_dict["C1_AC_up"]

        return c1_dict
