# ---
# jupyter:
#   jupytext:
#     text_representation:
#       extension: .py
#       format_name: light
#       format_version: '1.5'
#       jupytext_version: 1.16.1
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# Simple test of using SaintVenantKirchhoff with 2nd PK stresses and Green Lagrange strains in a total Lagrangian formulation.
#
# 
#
# ## `FEniCSx` implementation
#
# We define a box mesh representing half of a beam oriented along the $x$-direction. The beam will be fully clamped on its left side and symmetry conditions will be imposed on its right extremity. The loading consists of a uniform self-weight.
#
# ```{image} finite_strain_plasticity_solution.png
# :align: center
# :width: 500px
# ```

# +
import numpy as np
import dolfinx as dolx
import matplotlib.pyplot as plt
import os
import ufl
import mgis.behaviour as mgis_bv
from petsc4py import PETSc
from mpi4py import MPI
from dolfinx import fem, mesh, io
from dolfinx.cpp.nls.petsc import NewtonSolver
from dolfinx_materials.quadrature_map import QuadratureMap
from dolfinx_materials.material.mfront import MFrontMaterial
from dolfinx_materials.solvers import NonlinearMaterialProblem
from dolfinx_materials.utils import (
    nonsymmetric_tensor_to_vector, symmetric_tensor_to_vector
)

dolx.log.set_log_level(dolx.log.LogLevel.INFO)


comm = MPI.COMM_WORLD
rank = comm.rank

current_path = os.getcwd()
print(current_path)

length, width = 1.0, 0.04
nx, ny = 10, 4
domain = mesh.create_rectangle(
    comm,
    [(0, -width / 2), (length, width / 2)],
    [nx, ny],
    cell_type=mesh.CellType.quadrilateral,
    ghost_mode=mesh.GhostMode.none,
)
gdim = domain.topology.dim
print(f'{gdim}D problem')

V = fem.functionspace(domain, ("P", 2, (gdim,)))


def left(x):
    return np.isclose(x[0], 0)


def right(x):
    return np.isclose(x[0], length)


left_dofs = fem.locate_dofs_geometrical(V, left)
V_x, _ = V.sub(0).collapse()
right_dofs = fem.locate_dofs_geometrical((V.sub(0), V_x), right)

uD = fem.Function(V_x)
bcs = [
    fem.dirichletbc(np.zeros((gdim,)), left_dofs, V),
    fem.dirichletbc(uD, right_dofs, V.sub(0)),
]

selfweight = fem.Constant(domain, np.zeros((gdim,)))

du = ufl.TrialFunction(V)
v = ufl.TestFunction(V)
u = fem.Function(V, name="Displacement")

print(u.ufl_shape, V)
# -

# The `MFrontMaterial` instance is loaded from the `MFront` `LogarithmicStrainPlasticity` behavior. This behavior is a finite-strain behavior (`material.is_finite_strain=True`) which relies on a kinematic description using the total deformation gradient $\boldsymbol{F}$. By default, a `MFront` behavior always returns the Cauchy stress as the stress measure after integration. However, the stress variable dual to the deformation gradient is the first Piola-Kirchhoff (PK1) stress. An internal option of the MGIS interface is therefore used in the finite-strain context to return the PK1 stress as the "flux" associated to the "gradient" $\boldsymbol{F}$. Both quantities are non-symmetric tensors, aranged as a 9-dimensional vector in 3D following [`MFront` conventions on tensors](https://thelfer.github.io/tfel/web/tensors.html).

material = MFrontMaterial(
    os.path.join(current_path, "src/libBehaviour.so"),
    "SaintVenantKirchhoffElasticity",
    hypothesis = 'plane_strain',
    material_properties={"YoungModulus":2e5,"PoissonRatio": 0.3},
    stress_measure = mgis_bv.FiniteStrainBehaviourOptionsStressMeasure.PK2,
    tangent_operator = mgis_bv.FiniteStrainBehaviourOptionsTangentOperator.DS_DEGL,
)

if rank == 0:
    print(material.behaviour.getBehaviourType())
    print(material.behaviour.getKinematic())
    print(material.gradient_names, material.gradient_sizes)
    print(material.flux_names, material.flux_sizes)


# In this large-strain setting, the `QuadratureMapping` acts from the deformation gradient $\boldsymbol{F}=\boldsymbol{I}+\nabla\boldsymbol{u}$ to the first Piola-Kirchhoff stress $\boldsymbol{P}$. We must therefore register the deformation gradient as `Identity(3)+grad(u)`.

# +
def F(u):
    return nonsymmetric_tensor_to_vector(ufl.Identity(gdim) + ufl.grad(u), 1.)


def dF(u):
    return nonsymmetric_tensor_to_vector(ufl.grad(u))

def dEgl(u,v):
    grad_v = ufl.grad(v)
    grad_u = ufl.grad(u)
    return symmetric_tensor_to_vector((1/2)*(grad_v+grad_v.T+ufl.dot(grad_u.T,grad_v)+ufl.dot(grad_v.T,grad_u)))


quadrature_degree = 2
qmap = QuadratureMap(domain, quadrature_degree, material)
qmap.register_gradient("DeformationGradient", F(u))
# -

# We will work in a Total Lagrangian formulation, writing the weak form of equilibrium on the reference configuration $\Omega_0$, thereby defining the nonlinear residual weak form as:
# Find $\boldsymbol{u}\in V$ such that:
#
# $$
# \int_{\Omega_0} \boldsymbol{P}(\boldsymbol{F}(\boldsymbol{u})):\nabla \boldsymbol{v} \,\text{d}\Omega - \int_{\Omega_0} \boldsymbol{f}\cdot\boldsymbol{v}\,\text{d}\Omega = 0 \quad \forall \boldsymbol{v}\in V
# $$
# where $\boldsymbol{f}$ is the self-weight.
#
# The corresponding Jacobian form is computed via automatic differentiation. As for the [small-strain elastoplasticity example](https://thelfer.github.io/mgis/web/mgis_fenics_small_strain_elastoplasticity.html), state variables include the `ElasticStrain` and `EquivalentPlasticStrain` since the same behavior is used as in the small-strain case with the only difference that the total strain is now given by the Hencky strain measure. In particular, the `ElasticStrain` is still a symmetric tensor (vector of dimension 6). Note that it has not been explicitly defined as a state variable in the `MFront` behavior file since this is done automatically when using the `IsotropicPlasticMisesFlow` parser.

#PK1 = qmap.fluxes["FirstPiolaKirchhoffStress"]
PK2 = qmap.fluxes["SecondPiolaKirchhoffStress"]
Res = (ufl.dot(PK2, dEgl(u,v)) - ufl.dot(selfweight, v)) * qmap.dx
#Res = (ufl.dot(PK1,dF(v))-ufl.dot(selfweight,v))*qmap.dx
Jac = qmap.derivative(Res, u, du)

# Finally, we setup the nonlinear problem, the corresponding Newton solver and solve the load-stepping problem.

# + tags=["hide-output"]
problem = NonlinearMaterialProblem(qmap, Res, Jac, u, bcs)

newton = NewtonSolver(comm)
newton.rtol = 1e-4
newton.atol = 1e-4
newton.convergence_criterion = "residual"
newton.report = True

# Set solver options
ksp = newton.krylov_solver
opts = PETSc.Options()
option_prefix = ksp.getOptionsPrefix()
#opts[f"{option_prefix}ksp_type"] = "bcgs"
#opts[f"{option_prefix}pc_type"] = "mg"
#opts[f"{option_prefix}ksp_atol"] = "1e-16"
#opts[f"{option_prefix}ksp_rtol"] = "1e-16"
#opts[f"{option_prefix}ksp_monitor"] = ""
opts[f"{option_prefix}ksp_type"] = "preonly"
opts[f"{option_prefix}pc_type"] = "lu"
opts[f"{option_prefix}pc_factor_mat_solver_type"] = "mumps"
ksp.setFromOptions()

Nincr = 30
load_steps = np.linspace(0.0, 1.0, Nincr + 1)

vtk = io.VTKFile(domain.comm, f"results/{material.name}.pvd", "w")
results = np.zeros((Nincr + 1, 2))
for i, t in enumerate(load_steps[1:]):
    selfweight.value[-1] = -50e3 * t

    converged, it = problem.solve(newton, print_solution=True)

    if rank == 0:
        print(f"Increment {i+1} converged in {it} iterations.")

    #p0 = qmap.project_on("GreenL", ("DG", 0))

    vtk.write_function(u, t)
    #vtk.write_function(p0, t)

    w = u.sub(1)
    local_max = max(np.abs(w.vector.array))
    # Perform the reduction to get the global maximum on rank 0
    global_max = MPI.COMM_WORLD.reduce(local_max, op=MPI.MAX, root=0)
    results[i + 1, 0] = global_max
    results[i + 1, 1] = t
vtk.close()
# -

# During the load incrementation, we monitor the evolution of the maximum vertical downwards displacement.
# The load-displacement curve exhibits a classical elastoplastic behavior rapidly followed by a stiffening behavior due to membrane catenary effects.

if rank==0:
    plt.figure()
    plt.plot(results[:, 0], results[:, 1], "-oC3")
    plt.xlabel("Displacement")
    plt.ylabel("Load")
    plt.savefig('./displacement_PK2_10x_4y.pdf',bbox_inches='tight', format = 'pdf', dpi =  600)
    plt.show()

# ## References
#
# ```{bibliography}
# :filter: docname in docnames
# ```
