"""
prepare.py: Functions for getting ready for geometry optimization

Copyright 2016-2020 Regents of the University of California and the Authors

Authors: Lee-Ping Wang, Chenchen Song

Contributors: Yudong Qiu, Daniel G. A. Smith, Josh Horton

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
this list of conditions and the following disclaimer in the documentation
and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors
may be used to endorse or promote products derived from this software
without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
"""

import itertools
import numpy as np

from .internal import Distance, Angle, Dihedral, CartesianX, CartesianY, CartesianZ, TranslationX, TranslationY, TranslationZ, RotationA, RotationB, RotationC
from .engine import set_tcenv, load_tcin, TeraChem, ConicalIntersection, Psi4, QChem, Gromacs, Molpro, OpenMM, QCEngineAPI
from .molecule import Molecule, Elements
from .nifty import logger, isint, uncommadash, bohr2ang, ang2bohr

def get_molecule_engine(**kwargs):
    """
    Parameters
    ----------
    args : namespace
        Command line arguments from argparse

    Returns
    -------
    Molecule
        Molecule object containing necessary optimization info
    Engine
        Engine object containing methods for calculating energy and gradient
    """
    ### Set up based on which quantum chemistry code we're using.
    engine_str = kwargs.get('engine', None)
    customengine = kwargs.get('customengine', None)
    # Path to Molpro executable (used if molpro=True)
    molproexe = kwargs.get('molproexe', None)
    # PDB file will be read for residue IDs to make TRICs for fragments
    # and provide starting coordinates in the case of OpenMM
    pdb = kwargs.get('pdb', None)
    # if frag=True, do not add a bond between residues.
    frag = kwargs.get('frag', False)
    # Number of threads to use (engine-dependent)
    threads = kwargs.get('nt', None)
    # Name of the input file.
    inputf = kwargs.get('input')
    # Name of temporary directory for calculations, needed by some engines.
    dirname = kwargs.get('dirname', None)
    # Temporary directory generated by a previous Q-Chem calculation, may be used at the beginning of a geomeTRIC calculation
    qcdir = kwargs.get('qcdir', None)

    ## MECI calculations create a custom engine that contains two other engines.
    if kwargs.get('meci', None):
        if engine_str.lower() in ['psi4', 'gmx', 'molpro', 'qcengine', 'openmm'] or customengine:
            logger.warning("MECI optimizations are not tested with engines: psi4, gmx, molpro, qcegine, openmm, customengine. Be Careful!")
        ## If 'engine' is provided as the argument to 'meci', then we assume the engine is
        # directly returning the MECI objective function and gradient.
        if kwargs['meci'].lower() == 'engine':
            sub_kwargs = kwargs.copy()
            sub_kwargs['meci'] = None
            M, engine = get_molecule_engine(**sub_kwargs)
        else:
            meci_sigma = kwargs.get('meci_sigma')
            meci_alpha = kwargs.get('meci_alpha')
            sub_engines = {}
            for state in [1, 2]:
                sub_kwargs = kwargs.copy()
                if state == 2:
                    sub_kwargs['input'] = kwargs['meci']
                sub_kwargs['meci'] = None
                M, sub_engine = get_molecule_engine(**sub_kwargs)
                sub_engines[state] = sub_engine
            engine = ConicalIntersection(M, sub_engines[1], sub_engines[2], meci_sigma, meci_alpha)
        return M, engine

    ## Read radii from the command line.
    # Ions should have radii of zero.
    arg_radii = kwargs.get('radii', ["Na","0.0","Cl","0.0","K","0.0"])
    if (len(arg_radii) % 2) != 0:
        raise RuntimeError("Must have an even number of arguments for radii")
    nrad = int(len(arg_radii) / 2)
    radii = {}
    for i in range(nrad):
        radii[arg_radii[2*i].capitalize()] = float(arg_radii[2*i+1])

    using_qchem = False
    threads_enabled = False
    if engine_str:
        engine_str = engine_str.lower()
        if engine_str not in ['tera', 'qchem', 'psi4', 'gmx', 'molpro', 'openmm', 'qcengine']:
            raise RuntimeError("Valid values of engine are: tera, qchem, psi4, gmx, molpro, openmm, qcengine")
        if customengine:
            raise RuntimeError("engine and customengine cannot simultaneously be set")
        if engine_str == 'tera':
            logger.info("TeraChem engine selected. Expecting TeraChem input for gradient calculation.\n")
            set_tcenv()
            tcin = load_tcin(inputf)
            # The QM-MM interface is designed on the following ideas:
            # 1) We are only optimizing the QM portion of the system 
            # (until we implement fast inversion of G matrices and Hessians)
            # 2) The geomeTRIC optimizer only "sees" the part of the molecule being optimized.
            # 3) The TeraChem engine writes .rst7 files instead of .xyz files by inserting the
            # optimization coordinates into the correct locations.
            qmmm = 'qmindices' in tcin
            if qmmm:
                from simtk.openmm.app import AmberPrmtopFile
                # Need to build a molecule object for the portion of the system being optimized
                # We rely on OpenMM's AmberPrmtopFile class to read the .prmtop file
                if not os.path.exists(tcin['coordinates']):
                    raise RuntimeError("TeraChem QM/MM coordinate file does not exist")
                if not os.path.exists(tcin['prmtop']):
                    raise RuntimeError("TeraChem QM/MM prmtop file does not exist")
                if not os.path.exists(tcin['qmindices']):
                    raise RuntimeError("TeraChem QM/MM qmindices file does not exist")
                prmtop_name = tcin['prmtop']
                prmtop = AmberPrmtopFile(prmtop_name)
                M_full = Molecule(tcin['coordinates'], ftype='inpcrd', build_topology=False)
                M_full.elem = [a.element.symbol for a in list(prmtop.topology.atoms())]
                M_full.resid = [a.residue.index for a in list(prmtop.topology.atoms())]
                qmindices_name = tcin['qmindices']
                qmindices = [int(i.split()[0]) for i in open(qmindices_name).readlines()]
                M = M_full.atom_select(qmindices)
                M.top_settings['radii'] = radii
                M.top_settings['fragment'] = frag
                M.build_topology()
            elif pdb is not None:
                M = Molecule(pdb, radii=radii, fragment=frag)
            else:
                if not os.path.exists(tcin['coordinates']):
                    raise RuntimeError("TeraChem coordinate file does not exist")
                M = Molecule(tcin['coordinates'], radii=radii, fragment=frag)
            M.charge = tcin['charge']
            M.mult = tcin.get('spinmult',1)
            # The TeraChem engine needs to write rst7 files before calling TC
            # and also make sure the prmtop and qmindices.txt files are present.
            engine = TeraChem(M, tcin, dirname=dirname)
        elif engine_str == 'qchem':
            logger.info("Q-Chem engine selected. Expecting Q-Chem input for gradient calculation.\n")
            # The file from which we make the Molecule object
            if pdb is not None:
                # If we pass the PDB, then read both the PDB and the Q-Chem input file,
                # then copy the Q-Chem rem variables over to the PDB
                M = Molecule(pdb, radii=radii, fragment=frag)
                M1 = Molecule(inputf, radii=radii)
                for i in ['qctemplate', 'qcrems', 'elem', 'qm_ghost', 'charge', 'mult']:
                    if i in M1: M[i] = M1[i]
            else:
                M = Molecule(inputf, radii=radii)
            engine = QChem(M, dirname=dirname, qcdir=qcdir, threads=threads)
            using_qchem = True
            threads_enabled = True
        elif engine_str == 'gmx':
            logger.info("Gromacs engine selected. Expecting conf.gro, topol.top and shot.mdp (exact names).\n")
            M = Molecule(inputf, radii=radii, fragment=frag)
            if pdb is not None:
                M = Molecule(pdb, radii=radii, fragment=frag)
            if 'boxes' in M.Data:
                del M.Data['boxes']
            engine = Gromacs(M)
        elif engine_str == 'openmm':
            logger.info("OpenMM engine selected. Expecting forcefield.xml or system.xml file, and PDB passed in via --pdb.\n")
            if pdb is None:
                raise RuntimeError("Must pass a PDB with option --pdb to use OpenMM.")
            M = Molecule(pdb, radii=radii, fragment=frag)
            if 'boxes' in M.Data:
                del M.Data['boxes']
            engine = OpenMM(M, pdb, inputf)
        elif engine_str == 'psi4':
            logger.info("Psi4 engine selected. Expecting Psi4 input for gradient calculation.\n")
            engine = Psi4(threads=threads)
            engine.load_psi4_input(inputf)
            if pdb is not None:
                M = Molecule(pdb, radii=radii, fragment=frag)
                M1 = engine.M
                for i in ['elem']:
                    if i in M1: M[i] = M1[i]
            else:
                M = engine.M
                M.top_settings['radii'] = radii
            threads_enabled = True
        elif engine_str == 'molpro':
            logger.info("Molpro engine selected. Expecting Molpro input for gradient calculation.\n")
            engine = Molpro(threads=threads)
            engine.load_molpro_input(inputf)
            M = engine.M
            if molproexe is not None:
                engine.set_molproexe(molproexe)
            threads_enabled = True
        elif engine_str == 'qcengine':
            logger.info("QCEngine selected.\n")
            schema = kwargs.get('qcschema', False)
            if schema is False:
                raise RuntimeError("QCEngineAPI option requires a QCSchema")
    
            program = kwargs.get('qce_program', False)
            if program is False:
                raise RuntimeError("QCEngineAPI option requires a qce_program option")
            engine = QCEngineAPI(schema, program)
            M = engine.M
        else:
            raise RuntimeError("Failed to create an engine object, this might be a bug in get_molecule_engine")
    elif customengine:
        logger.info("Custom engine selected.\n")
        engine = customengine
        M = engine.M
    else:
        raise RuntimeError("Neither engine name nor customengine object was provided.\n")
    
    # If --coords is provided via command line, use initial coordinates in the provided file
    # to override all previously provided coordinates.
    arg_coords = kwargs.get('coords', None)
    if arg_coords is not None:
        M1 = Molecule(arg_coords)
        M1 = M1[-1]
        M.xyzs = M1.xyzs

    # Perform some sanity checks on arguments
    if not using_qchem and qcdir:
        raise EngineError("qcdir keyword argument passed to get_molecule_engine but Q-Chem engine is not being used")
    if threads and not threads_enabled:
        raise RuntimeError("Setting number of threads not configured to work with %s yet" % engine_str)

    return M, engine

def one_dimensional_scan(init, final, steps):
    """
    Return a list of N equally spaced values between initial and final.
    This method works with lists of numbers

    Parameters
    ----------
    init : list
        List of numbers to be interpolated
    final : np.ndarray or list
        List of final numbers, must have same shape as "init"
    steps : int
        Number of interpolation steps

    Returns
    -------
    list
        List of lists that interpolate between init and final, including endpoints.
    """
    if len(init) != len(final):
        raise RuntimeError("init and final must have the same length")
    Answer = []
    for j in range(len(init)):
        Answer.append(np.linspace(init[j], final[j], steps))
    Answer = list([list(i) for i in np.array(Answer).T])
    return Answer


def parse_constraints(molecule, constraints_string):
    """
    Parameters
    ----------
    molecule : Molecule
        Molecule object
    constraints_string : str
        String containing the constraint specification.

    Returns
    -------
    objs : list
        List of primitive internal coordinates corresponding to the constraints
    valgrps : list
        List of lists of constraint values. (There are multiple lists when we are scanning)
    """
    mode = None
    Freezes = []
    # The key in this dictionary is for looking up the following information:
    # 1) The classes for creating the primitive coordinates corresponding to the constraint
    # 2) The number of atomic indices that are required to specify the constraint
    ClassDict = {"distance":([Distance], 2),
                 "angle":([Angle], 3),
                 "dihedral":([Dihedral], 4),
                 "x":([CartesianX], 1),
                 "y":([CartesianY], 1),
                 "z":([CartesianZ], 1),
                 "xy":([CartesianX, CartesianY], 1),
                 "xz":([CartesianX, CartesianZ], 1),
                 "yz":([CartesianY, CartesianZ], 1),
                 "xyz":([CartesianX, CartesianY, CartesianZ], 1),
                 "trans-x":([TranslationX], 1),
                 "trans-y":([TranslationY], 1),
                 "trans-z":([TranslationZ], 1),
                 "trans-xy":([TranslationX, TranslationY], 1),
                 "trans-xz":([TranslationX, TranslationZ], 1),
                 "trans-yz":([TranslationY, TranslationZ], 1),
                 "trans-xyz":([TranslationX, TranslationY, TranslationZ], 1),
                 "rotation":([RotationA, RotationB, RotationC], 1)
                 }
    AtomKeys = ["x", "y", "z", "xy", "yz", "xz", "xyz"]
    TransKeys = ["trans-x", "trans-y", "trans-z", "trans-xy", "trans-yz", "trans-xz", "trans-xyz"]
    objs = []
    vals = []
    coords = molecule.xyzs[0].flatten() * ang2bohr
    for line in constraints_string.split('\n'):
        line = line.split("#")[0].strip().lower()
        # This is a list-of-lists. The intention is to create a multidimensional grid
        # of constraint values if necessary.
        if len(line) == 0: continue
        logger.info(line+'\n')
        if line.startswith("$"):
            mode = line.replace("$","")
        else:
            if mode is None:
                raise RuntimeError("Mode ($freeze, $set, $scan) must be set before specifying any constraints")
            s = line.split()
            key = s[0]
            if ''.join(sorted(key)) in AtomKeys:
                key = ''.join(sorted(key))
            elif ''.join(sorted(key.replace('trans-',''))) in AtomKeys:
                key = 'trans-'+''.join(sorted(key.replace('trans-','')))
            classes, n_atom = ClassDict[key]
            if mode == "freeze":
                ntok = n_atom
            elif mode == "set":
                if key == 'rotation':
                    ntok = n_atom + 4
                else:
                    ntok = n_atom + len(classes)
            elif mode == "scan":
                if key == 'rotation':
                    ntok = n_atom + 6
                else:
                    ntok = n_atom + 2*len(classes) + 1
            if len(s) != (ntok+1):
                raise RuntimeError("For this line:%s\nExpected %i tokens but got %i" % (line, ntok+1, len(s)))
            if key in AtomKeys or key in TransKeys:
                # Special code that works for atom position and translation constraints.
                if isint(s[1]):
                    atoms = [int(s[1])-1]
                elif s[1] in [k.lower() for k in Elements]:
                    atoms = [i for i in range(molecule.na) if molecule.elem[i].lower() == s[1]]
                else:
                    atoms = uncommadash(s[1])
                if any([i<0 for i in atoms]):
                    raise RuntimeError("Atom numbers must start from 1")
                if any([i>=molecule.na for i in atoms]):
                    raise RuntimeError("Constraints refer to higher atom indices than the number of atoms")
            if key in AtomKeys:
                # The x-coordinate of all the atoms in a group is a
                # list of constraints that is scanned in 1-D.
                for cls in classes:
                    objs.append([cls(a, w=1.0) for a in atoms])
                if mode == "freeze":
                    for cls in classes:
                        vals.append([[None for a in atoms]])
                elif mode == "set":
                    x1 = [float(i) * ang2bohr for i in s[2:2+len(classes)]]
                    for icls, cls in enumerate(classes):
                        vals.append([[x1[icls] for a in atoms]])
                elif mode == "scan":
                    # If we're scanning it, then we add the whole list of distances to the list-of-lists
                    x1 = [float(i) * ang2bohr for i in s[2:2+len(classes)]]
                    x2 = [float(i) * ang2bohr for i in s[2+len(classes):2+2*len(classes)]]
                    nstep = int(s[2+2*len(classes)])
                    valscan = one_dimensional_scan(x1, x2, nstep)
                    for icls, cls in enumerate(classes):
                        vals.append([[v[icls] for a in atoms] for v in valscan])
            elif key in TransKeys:
                # If there is more than one atom and the mode is "set" or "scan", then the
                # center of mass is constrained, so we pick the corresponding classes.
                if len(atoms) > 1:
                    objs.append([cls(atoms, w=np.ones(len(atoms))/len(atoms)) for cls in classes])
                else:
                    objs.append([cls(atoms[0], w=1.0) for cls in classes])
                if mode == "freeze":
                    # LPW 2016-02-10:
                    # trans-x, trans-y, trans-z is a GROUP of constraints
                    # Each group of constraints gets a [[None, None, None]] appended to vals
                    vals.append([[None for cls in classes]])
                elif mode == "set":
                    # Depending on how many coordinates are constrained, we read in the corresponding
                    # number of constraint values.
                    x1 = [float(i) * ang2bohr for i in s[2:2+len(classes)]]
                    # If there's just one constraint value then we append it to the value list-of-lists
                    vals.append([x1])
                elif mode == "scan":
                    # If we're scanning it, then we add the whole list of distances to the list-of-lists
                    x1 = [float(i) * ang2bohr for i in s[2:2+len(classes)]]
                    x2 = [float(i) * ang2bohr for i in s[2+len(classes):2+2*len(classes)]]
                    nstep = int(s[2+2*len(classes)])
                    vals.append(one_dimensional_scan(x1, x2, nstep))
            elif key in ["distance", "angle", "dihedral"]:
                if len(classes) != 1:
                    raise RuntimeError("Not OK!")
                atoms = [int(i)-1 for i in s[1:1+n_atom]]
                if key == "distance" and atoms[0] > atoms[1]:
                    atoms = atoms[::-1]
                if key == "angle" and atoms[0] > atoms[2]:
                    atoms = atoms[::-1]
                if key == "dihedral" and atoms[1] > atoms[2]:
                    atoms = atoms[::-1]
                if any([i<0 for i in atoms]):
                    raise RuntimeError("Atom numbers must start from 1")
                if any([i>=molecule.na for i in atoms]):
                    raise RuntimeError("Constraints refer to higher atom indices than the number of atoms")
                objs.append([classes[0](*atoms)])
                if mode == "freeze":
                    vals.append([[None]])
                elif mode in ["set", "scan"]:
                    if key == "distance": x1 = float(s[1+n_atom]) * ang2bohr
                    else: x1 = float(s[1+n_atom])*np.pi/180.0
                    if mode == "set":
                        vals.append([[x1]])
                    else:
                        if key == "distance": x2 = float(s[2+n_atom]) * ang2bohr
                        else: x2 = float(s[2+n_atom])*np.pi/180.0
                        nstep = int(s[3+n_atom])
                        vals.append([[i] for i in list(np.linspace(x1,x2,nstep))])
            elif key in ["rotation"]:
                # User can only specify ranges of atoms
                atoms = uncommadash(s[1])
                sel = coords.reshape(-1,3)[atoms,:]  * ang2bohr
                sel -= np.mean(sel, axis=0)
                rg = np.sqrt(np.mean(np.sum(sel**2, axis=1)))
                if mode == "freeze":
                    for cls in classes:
                        objs.append([cls(atoms, coords, {}, w=rg)])
                        vals.append([[None]])
                elif mode in ["set", "scan"]:
                    objs.append([cls(atoms, coords, {}, w=rg) for cls in classes])
                    # Get the axis
                    u = np.array([float(s[i]) for i in range(2, 5)])
                    u /= np.linalg.norm(u)
                    # Get the angle
                    theta1 = float(s[5]) * np.pi / 180
                    if np.abs(theta1) > np.pi * 0.9:
                        logger.info("Large rotation: Your constraint may not work\n")
                    if mode == "set":
                        c = np.cos(theta1/2.0)
                        s = np.sin(theta1/2.0)
                        q = np.array([c, u[0]*s, u[1]*s, u[2]*s])
                        fac, _ = calc_fac_dfac(c)
                        v1 = fac*q[1]*rg
                        v2 = fac*q[2]*rg
                        v3 = fac*q[3]*rg
                        vals.append([[v1, v2, v3]])
                    elif mode == "scan":
                        theta2 = float(s[6]) * np.pi / 180
                        if np.abs(theta2) > np.pi * 0.9:
                            logger.info("Large rotation: Your constraint may not work\n")
                        steps = int(s[7])
                        # To alleviate future confusion:
                        # There is one group of three constraints that we are going to scan over in one dimension.
                        # Here we create one group of constraint values.
                        # We will add triplets of constraint values to this group
                        vs = []
                        for theta in np.linspace(theta1, theta2, steps):
                            c = np.cos(theta/2.0)
                            s = np.sin(theta/2.0)
                            q = np.array([c, u[0]*s, u[1]*s, u[2]*s])
                            fac, _ = calc_fac_dfac(c)
                            v1 = fac*q[1]*rg
                            v2 = fac*q[2]*rg
                            v3 = fac*q[3]*rg
                            vs.append([v1, v2, v3])
                        vals.append(vs)
    if len(objs) != len(vals):
        raise RuntimeError("objs and vals should be the same length")
    valgrps = [list(itertools.chain(*i)) for i in list(itertools.product(*vals))]
    objs = list(itertools.chain(*objs))
    return objs, valgrps

