import os
import time
import glob
import shutil
import numpy as np
import pandas as pd
from pathlib import Path
from random import random
from pysagas import banner
import matplotlib.pyplot as plt
from pysagas.wrappers import Cart3DWrapper
from hypervehicle.generator import Generator
from typing import List, Dict, Optional, Optional, Callable, Tuple
from hypervehicle.utilities import SensitivityStudy, append_sensitivities_to_tri


np.seterr(all="ignore")


class ShapeOpt:
    """A wrapper to perform shape optimisation with Cart3D."""

    C3D_errors = [
        "==> ADAPT failed",
        "Check cart3d.out in AD_A_J for more clues",
        "==> adjointErrorEst_quad failed again, status = 1",
        "ERROR: CUBES failed",
        "ERROR: ADAPT failed with status = 1",
        "ERROR",
    ]

    def __init__(
        self,
        home_dir: str,
        a_inf: float,
        rho_inf: float,
        V_inf: float,
        A_ref: float,
        generator: Generator,
        sensitivity_filename: str = "all_components_sensitivity.csv",
        working_dir_name: str = "working_dir",
        sim_dir_name: str = "simulation",
        basefiles_dir_name: str = "basefiles",
        c3d_logname: str = "C3D_log",
        c3d_info_file: str = None,
        matching_tolerance: float = 1e-5,
    ) -> None:
        # Construct paths
        self.root_dir = home_dir
        self.basefiles_dir = os.path.join(home_dir, basefiles_dir_name)
        self.working_dir = os.path.join(home_dir, working_dir_name)
        self.sim_dir_name = sim_dir_name
        self.sensitivity_filename = sensitivity_filename
        self.completion_filename = "ITERATION_COMPLETE"
        self.parameters_filename = "parameters.csv"
        self.f_sense_filename = "F_sensitivities.csv"
        self.jacobian_filename = "jacobian.csv"
        self.objective_filename = "objective.txt"

        self.generator = generator

        self.c3d_logname = c3d_logname
        self.minimum_volume = None

        # Save callback function
        # TODO - implement standard functions (eg. min drag, L/D, etc.)
        self._obj_jac_cb = None

        # TODO - pass as flowstate
        self.rho_inf = rho_inf
        self.V_inf = V_inf
        self.a_inf = a_inf
        self.A_ref = A_ref

        # Create instance of Cart3D prepper
        self._c3dprepper = C3DPrep(logfile=c3d_logname, info_file=c3d_info_file)

        # Other settings
        self._matching_tolerance = matching_tolerance
        self._max_matching_tol = 0.1
        self._matching_target = 0.9

    def _prepare(self, warmstart: bool, param_names: List[str]):
        """Prepares the working directory for the optimisation
        problem. Checks to see which iteration the solver is up to,
        and creates a new iteration directory if required.
        """
        # Initialise 'older' results
        x_older = None
        jac_older = None

        # Check base files directory exist
        if not os.path.exists(self.basefiles_dir):
            raise Exception("Cart3D base file directory does not exist!")

        # Create working directory
        if not os.path.exists(self.working_dir):
            os.mkdir(self.working_dir)

        # Create heirarchy for this run
        iteration_dirs = [
            int(i)
            for i in os.listdir(self.working_dir)
            if os.path.isdir(os.path.join(self.working_dir, i))
        ]
        if len(iteration_dirs) > 0:
            # A previous iteration has run, check for completion file
            if os.path.exists(
                os.path.join(
                    self.working_dir,
                    f"{int(max(iteration_dirs)):04d}",
                    self.completion_filename,
                )
            ):
                # The latest iteration ran to completion
                if warmstart:
                    # Warmstarting, load results from this iteration
                    current_iter = max(iteration_dirs)
                    print(f"\n\x1B[3mWarmstarting from iteration {current_iter}\x1B[0m")

                else:
                    # Start next iteration
                    current_iter = max(iteration_dirs) + 1
                    print(f"\n\x1B[3mMoving onto iteration {current_iter}\x1B[0m")

            else:
                # This iteration did not complete, try resume it
                current_iter = max(iteration_dirs)
                print(f"\n\x1B[3mResuming from iteration {current_iter}\x1B[0m")

            # Look for x_older and jac_older
            prev_iter_dir = os.path.join(self.working_dir, f"{int(current_iter-1):04d}")
            x_older_path = os.path.join(prev_iter_dir, self.parameters_filename)
            jac_older_path = os.path.join(prev_iter_dir, self.jacobian_filename)
            if os.path.exists(x_older_path):
                # Older iteration directory exists, pick up x_older
                x_older = pd.read_csv(x_older_path, index_col=0).values

                # Also load previous jacobian
                jac_df = pd.read_csv(jac_older_path, index_col=0)["0"]
                jac_older = jac_df.loc[param_names].values

        else:
            # First iteration
            current_iter = 0

        # Print iteration header
        its = f"Iteration {int(current_iter)}".center(43, " ")
        print(f"{'':=>43}\n{its}\n{'':=>43}")

        # Define the current iteration directory
        iter_dir = os.path.join(self.working_dir, f"{int(current_iter):04d}")
        if not os.path.exists(iter_dir):
            # Create the directory
            os.mkdir(iter_dir)

        return iter_dir, x_older, jac_older

    def _run_sensitivity_study(
        self, iter_dir: str, param_names: List[str], x: List[float]
    ):
        """Runs the geometric sensitivity study for the specified iteration
        directory iter_dir. This method will produce the base geometry STL
        files, and the sensitivity files.
        """
        # Change into iteration directory
        os.chdir(iter_dir)

        # Check if sensitivity study has been run
        sens_files = glob.glob(f"{iter_dir}{os.sep}*sensitivity*")
        if len(sens_files) == 0:
            print("Running sensitivity study.")

            # Run the sensitivity study and generate the nominal geometry
            parameters = dict(zip(param_names, x))

            # Save these parameters for future reference
            pd.Series(parameters).to_csv(
                os.path.join(iter_dir, self.parameters_filename)
            )

            # Run sensitivity study
            ss = SensitivityStudy(vehicle_constructor=self.generator)
            ss.dvdp(parameter_dict=parameters, perturbation=2, write_nominal_stl=True)
            ss.to_csv()

        else:
            print("Sensitivity study already run.")

    def _run_simulation(
        self,
        basefiles_dir: str,
        iter_dir: str,
        max_adapt: int = None,
        warmstart: bool = False,
    ):
        """Prepare and run the CFD simulation with Cart3D. The simulation will be
        run in the 'simulation' subdirectory of the iteration directory. If warmstart
        is True, the previous iteration will be used to warm-start the solution.
        """
        # Make simulation directory
        sim_dir = os.path.join(iter_dir, self.sim_dir_name)
        run_intersect = False
        components_filepath = os.path.join(sim_dir, "Components.i.tri")
        if not os.path.exists(sim_dir):
            os.mkdir(sim_dir)
            run_intersect = True
        else:
            # Check for intersected file
            run_intersect = not os.path.exists(components_filepath)
            intersected = True
            print("Intersected components located.")

        # Attempt component intersection
        N = 3
        for attempt in range(N):
            # Run intersect
            if run_intersect:
                self._c3dprepper._log(f"SHAPEOPT INTERSECT ATTEMPT {attempt+1}")
                intersected = self._c3dprepper.intersect_stls()

            # Check for intersection
            if intersected:
                # Prepare rest of simulation directory
                if not os.path.exists(os.path.join(sim_dir, "input.cntl")):
                    # Prepare remaining C3D files
                    if warmstart:
                        # Copy necessary files from warm-start directory
                        warm_iter_dir = (
                            Path(iter_dir)
                            .parent.joinpath(f"{int(Path(iter_dir).name)-1:04d}")
                            .as_posix()
                        )
                        checkpoint_filename, commands = self._copy_warmstart_files(
                            warm_iter_dir, iter_dir
                        )

                    else:
                        # Move files to simulation directory (including Components.i.tri)
                        self._c3dprepper.run_autoinputs()
                        os.system(
                            f"mv *.tri Config.xml input.c3d preSpec.c3d.cntl {sim_dir} >> {self.c3d_logname} 2>&1"
                        )

                        # Copy sim files
                        os.system(
                            f"cp {basefiles_dir}/input.cntl {basefiles_dir}/aero.csh {sim_dir} >> {self.c3d_logname} 2>&1"
                        )

                        # Modify iteration aero.csh using max_adapt
                        if max_adapt:
                            self._overwrite_adapt(sim_dir, max_adapt)

                # Create all_components_sensitivity.csv
                if not os.path.exists(self.sensitivity_filename):
                    self._combine_sense_data(
                        components_filepath,
                        sensitivity_files=glob.glob("*sensitivity*"),
                        match_target=self._matching_target,
                        tol_0=self._matching_tolerance,
                        max_tol=self._max_matching_tol,
                    )

                # Override warmstart flag
                if os.path.exists(os.path.join(sim_dir, "aero.csh")) and warmstart:
                    # Warmstart set to True, but aero.csh file present
                    warmstart = False

                # Run Cart3D and await result
                os.chdir(sim_dir)
                if warmstart:
                    # Define wait files and run command
                    c3d_donefile = os.path.join(sim_dir, "loadsCC.dat")
                    # TODO - need to handle the case when commands isn't defined here, due to restart
                    run_cmd = commands["flowCart"]
                else:
                    target_adapt = self._infer_adapt(sim_dir)
                    c3d_donefile = os.path.join(sim_dir, target_adapt, "FLOW", "DONE")
                    run_cmd = "./aero.csh restart"

                _restarts = 0
                if not os.path.exists(c3d_donefile):
                    # Cart3D has not started / didn't finish
                    print(
                        "\nStarting Cart3D, awaiting",
                        os.sep.join(c3d_donefile.split(os.sep)[-6:]),
                    )

                    if warmstart:
                        # Prepare for warm-start
                        os.system(
                            f"{commands['cubes']} -remesh >> {self.c3d_logname} 2>&1"
                        )
                        os.system(f"{commands['mgPrep']} >> {self.c3d_logname} 2>&1")

                        os.system(
                            f"mesh2mesh -v -m1 refMesh.mg.c3d -m2 Mesh.mg.c3d -q1 {checkpoint_filename} -q2 Restart.file >> {self.c3d_logname} 2>&1"
                        )

                    _start = time.time()
                    os.system(f"./aero.csh restart >> {self.c3d_logname} 2>&1")
                    while not os.path.exists(c3d_donefile):
                        # Wait...
                        time.sleep(5)

                        # Check for C3D failure
                        running, e = self._c3d_running()

                        if not running:
                            # C3D failed, try restart it
                            if _restarts > 3:
                                print("Too many Cart3D failures... Something is wrong.")
                                return False

                            print(f"\033[1mERROR\033[0m: Cart3D failed with error {e}")
                            print("  Restarting Cart3D.")
                            os.system(f"{run_cmd} >> {self.c3d_logname} 2>&1")
                            _restarts += 1

                    _end = time.time()
                    print(f"Cart3D simulations complete in {(_end-_start):.2f} s.")

                else:
                    # Cart3D already finished for this iteration
                    print("Cart3D DONE file located.")

                return True

            else:
                if attempt < N - 1:
                    print("Could not intersect components. Trying again.")
                else:
                    print("Could not intersect components. Exiting.")
                    return False

    def _calculate_penalty(self):
        """Calculate the objective function penalty due to constraint
        violation of input parameters.
        """
        # TODO - implement
        # What dimensionality should this take? Ideally same as the parameter
        # space. But what if the constraint is on a non-controlled variable
        # (eg. volume)? Then the penalty should be applied to all parameters?
        # But what if one parameter influences the volume more than the
        # others...
        penalty = 0
        return penalty

    def _process_results(self, param_names: List[str], iter_dir: str):
        # Construct simulation directory
        sim_dir = os.path.join(iter_dir, self.sim_dir_name)
        target_adapt = self._infer_adapt(sim_dir)

        # Extract drag coefficient of geometry
        if os.path.exists(os.path.join(sim_dir, target_adapt, "FLOW")):
            # Adaptation simulation
            loads_filepath = os.path.join(sim_dir, target_adapt, "FLOW", "loadsCC.dat")
            components_filepath = os.path.join(
                sim_dir, target_adapt, "FLOW/Components.i.plt"
            )
        else:
            # Possibly warm-started simulation
            loads_filepath = os.path.join(sim_dir, "loadsCC.dat")
            components_filepath = os.path.join(sim_dir, "Components.i.plt")
        loads_dict = self._read_c3d_loads(loads_filepath)

        # Approximate flow sensitivities
        f_sense_filename = os.path.join(iter_dir, self.f_sense_filename)
        if not os.path.exists(f_sense_filename):
            # Filepaths
            sensitivity_filepath = os.path.join(iter_dir, self.sensitivity_filename)

            # Create PySAGAS wrapper and run
            print("\nEvaluating sensitivities.")
            try:
                wrapper = Cart3DWrapper(
                    a_inf=self.a_inf,
                    rho_inf=self.rho_inf,
                    sensitivity_filepath=sensitivity_filepath,
                    components_filepath=components_filepath,
                    verbosity=0,
                )

            except ValueError:
                # The sensitivity data does not match the point data, regenerate it
                tri_components_filepath = os.path.join(sim_dir, "Components.i.tri")
                sensitivity_files = glob.glob(os.path.join(iter_dir, "*sensitivity*"))
                self._combine_sense_data(
                    tri_components_filepath,
                    sensitivity_files=sensitivity_files,
                    match_target=self._matching_target,
                    tol_0=self._matching_tolerance,
                    max_tol=self._max_matching_tol,
                    outdir=iter_dir,
                )

                # Re-instantiate the wrapper
                wrapper = Cart3DWrapper(
                    a_inf=self.a_inf,
                    rho_inf=self.rho_inf,
                    sensitivity_filepath=sensitivity_filepath,
                    components_filepath=components_filepath,
                    verbosity=0,
                )

            F_sense, _ = wrapper.calculate()
            print("  Done.")

            # Save F_sense
            F_sense.to_csv(f_sense_filename)

        else:
            # Load F_sense
            F_sense = pd.read_csv(f_sense_filename, index_col=0)
            print("Force sensitivities loaded from file.")

        # Non-dimensionalise
        coef_sens = F_sense / (0.5 * self.rho_inf * self.A_ref * self.V_inf**2)

        # Load parameter values
        x_df = pd.read_csv(
            os.path.join(iter_dir, self.parameters_filename), index_col=0
        )
        x = x_df.loc[param_names]["0"].values

        # Get objective function and Jacobian
        jacobian_filepath = os.path.join(iter_dir, self.jacobian_filename)
        if not os.path.exists(jacobian_filepath):
            # Load data
            properties_dir = glob.glob(f"{iter_dir}{os.sep}*_properties")[0]
            scalar_sens_dir = os.path.join(iter_dir, "scalar_sensitivities")
            vm = pd.read_csv(
                glob.glob(os.path.join(properties_dir, "*volmass.csv"))[0], index_col=0
            )["0"]
            vm_sens = pd.read_csv(
                os.path.join(scalar_sens_dir, "volmass_sensitivity.csv"), index_col=0
            )[param_names]

            # Call function
            obj, jac_df = self._obj_jac_cb(
                param_names=param_names,
                coef_sens=coef_sens,
                loads_dict=loads_dict,
                volmass=vm,
                volmass_sens=vm_sens,
            )

            # TODO - dimensionality check?

            # Save
            with open(os.path.join(iter_dir, self.objective_filename), "w") as f:
                f.write(f"objective: {obj}\n")
            jac_df.to_csv(jacobian_filepath)

        else:
            # Load from file
            with open(os.path.join(iter_dir, self.objective_filename), "r") as f:
                lines = f.readlines()
                obj = float(lines[0].strip().split(":")[-1])
            jac_df = pd.read_csv(jacobian_filepath, index_col=0)["0"]

        # Extract ordered jacobian values
        jac = jac_df.loc[param_names].values

        # TODO - also return step size?

        return obj, jac, x

    def _read_c3d_loads(
        self,
        loadsCC_filepath: str,
        b_frame: bool = True,
        v_frame: bool = True,
        moments: bool = True,
    ) -> dict:
        load_dict = {}
        with open(loadsCC_filepath, "r") as file:
            for line in file:
                if line[0] == "#":
                    # Commented line, skip
                    continue

                # Remove linebreaks and multiple spaces
                line = " ".join(line.split())
                words = line.split(":")

                if len(words) == 0 or len(words) == 1:  # skip if empty line
                    continue

                text = words[0]
                number = float(words[1])
                word = text.split(" ")
                tag = word[0]
                coeff = word[-1]
                coeff = coeff[1:4]
                if b_frame is True:
                    if coeff in ["C_A", "C_Y", "C_N"]:  # get force in b_frame
                        load_dict["{0}-{1}".format(coeff, tag)] = number
                if v_frame is True:
                    if coeff in ["C_D", "C_S", "C_L"]:  # get force in v_frame
                        load_dict["{0}-{1}".format(coeff, tag)] = number
                if moments is True:
                    if coeff in ["C_l", "C_m", "C_n", "C_M"]:  # get moment coeff
                        load_dict["{0}-{1}".format(coeff, tag)] = number

        return load_dict

    def _iterate(
        self,
        x: List[float],
        param_names: List[str],
        warmstart: bool,
        gamma: float,
        max_adapt: int = None,
    ):
        """Wrapper function to perform an iteration of the
        shape optimisation problem.

        Steps involved:
            Generating geometry and geometric sensitivities
            Running Cart3D
            Approximating the gradient information
            Return the objective function and gradient.
        """
        # Prepare this iteration
        iter_dir, x_older, jac_older = self._prepare(warmstart, param_names)

        # Run the sensitivity study
        self._run_sensitivity_study(iter_dir, param_names, x)

        # Run simulation
        success = self._run_simulation(
            self.basefiles_dir,
            iter_dir,
            max_adapt,
            bool((warmstart and x_older is not None)),
        )

        if success:
            # Simulation completed successfully
            obj, jac, x = self._process_results(param_names, iter_dir)

            # Calculate constraint penalty
            # TODO - implement penalised constraints
            penalty = self._calculate_penalty()

            # Create completion file
            pd.Series(
                {
                    "objective": obj,
                    "penalty": penalty,
                    "gamma": gamma,
                    **dict(zip(param_names, x)),
                }
            ).to_csv(os.path.join(iter_dir, self.completion_filename))

        else:
            raise Exception("Simulation failed.")

        # Change back to root dir
        os.chdir(self.root_dir)

        return obj, jac, x, x_older, jac_older

    def _gradient_search(
        self,
        parameters: Dict[str, float],
        warmstart: bool = False,
        max_step: float = None,
        adapt_schedule: List[int] = None,
        max_iterations: int = 10,
    ):
        """Performs a steepest descent search.

        Parameters
        ----------
        parameters: Dict[str, float]
            A dictionary of geometric parameters to pass to the vehicle generator.
        warmstart : bool, optional
            If you are resuming a previous run, set to True. This will accelerate
            convergence by improving the step size. The default is False.
        max_step : float, optional
            The maximum step size to use when stepping. The default is None.
        adapt_schedule : List[int], optional
            The schedule to follow for Cart3D adapt cycles per optimisation
            iteraton. If None, the max_adapt parameter will be used from the
            base aero.csh file. The default is None.
        max_iterations : int, optional
            The maximum number of iterations to perform. The default is 10.
        """
        param_names = list(parameters.keys())
        x0 = list(parameters.values())

        # Define initial step size
        gamma = 0.05

        # Iteration parameters
        i = self._get_last_iteration(self.working_dir)
        tolerance = 1e-3
        change = 2 * tolerance
        obj_prev = 10 * tolerance
        bailout = False
        max_step = max_step if max_step is not None else 1e9

        while change > tolerance:
            if i + 1 > max_iterations:
                # Exit now
                bailout = True
                break

            # Start timer
            _start = time.time()

            # Get adapt number for this iteration
            if adapt_schedule:
                max_adapt = adapt_schedule[min(len(adapt_schedule) - 1, i)]
            else:
                # Do not adjust adapt cycle numbers
                max_adapt = None

            # Get objective and jacobian
            obj, jac, x_old, x_older, jac_older = self._iterate(
                x=x0,
                param_names=param_names,
                warmstart=warmstart,
                gamma=gamma,
                max_adapt=max_adapt,
            )

            # Check for non-zero Jacobian
            if np.linalg.norm(jac) == 0:
                print("\033[1mERROR\033[0m: Exiting due to zero-norm Jacobian.")
                bailout = True
                break

            # Calculate step size
            if x_older is not None:
                _gamma = (
                    np.linalg.norm((x_old - x_older) * (jac - jac_older))
                    / np.linalg.norm(jac - jac_older) ** 2
                )

                # Correct for nan
                if not np.isnan(_gamma):
                    # Update gamma
                    gamma = _gamma

            # Adjust gamma
            gamma = min(gamma, max_step)

            # Update x0
            x0 = x_old - gamma * jac

            # Calculate change
            change = abs((obj - obj_prev) / obj_prev)
            # TODO - detect divergence?

            # Break out of warmstart routine
            warmstart = False

            # Update iteration
            _end = time.time()
            i += 1
            obj_prev = obj
            jac_older = jac
            x_older = x_old

            # Print Information
            print("\nIteration complete:")
            print(f"Time to complete: {(_end-_start):.2f} s")
            print("Objective function:", obj)
            print("Step size:", gamma)
            print("New guess for next iteration:")
            print(pd.Series(x0, index=param_names, name="Parameters").to_string())
            print("")

        # Finished
        if not bailout:
            print(f"\nExited with change = {change}")

    def optimise(
        self,
        parameters: Dict[str, float],
        obj_jac_cb: Callable,
        constraints=None,
        minimum_volume: float = None,
        warmstart: bool = True,
        max_step: float = None,
        adapt_schedule: Optional[List[int]] = None,
        max_iterations: int = 10,
    ):
        """Performs a steepest descent search.

        Parameters
        ----------
        parameters: Dict[str, float]
            A dictionary of geometric parameters to pass to the vehicle generator.
        obj_jac_cb : Callable
            The function to generate the objective function and the Jacobian.
        constraints : optional
            The constraints to be applied as penalties to the objective. Not
            implemented yet.
        minimum_volume : float, optional
            The minimum allowable volume of the geometry. This will be enforced
            via a penalty function on the objective. The default is None.
        warmstart : bool, optional
            If you are resuming a previous run, set to True. This will accelerate
            convergence by improving the step size. The default is True.
        max_step : float, optional
            The maximum step size. If None, there will be no upper limit. The
            default is None.
        adapt_schedule : List[int], optional
            The schedule to follow for setting Cart3D adapt cycles for each
            iteration of the optimiser. For example, to run the first iteration
            to adapt01, the second to adapt03, and to adapt06 for all subsequent
            iterations, set adapt_schedule=[1, 3, 6]. If adapt_cycle=None, the
            adapt cycle will be static, to whatever it is set to in the basefile.
            The default is None.
        max_iterations : int, optional
            The maximum number of iterations to perform. The default is 10.
        """
        # TODO - allow automatic adpative adapt_schedule

        # Print banner
        banner()
        print("\033[4mCart3D Shape Optimisation\033[0m".center(50, " "))

        # Save loadsCC key for objective function
        self.minimum_volume = minimum_volume

        # Save objective jacobian callback function
        self._obj_jac_cb = obj_jac_cb

        # Run
        _opt_start = time.time()
        try:
            self._gradient_search(
                parameters=parameters,
                warmstart=warmstart,
                max_step=max_step,
                adapt_schedule=adapt_schedule,
                max_iterations=max_iterations,
            )
        except KeyboardInterrupt:
            # Change back to root dir and exit
            os.chdir(self.root_dir)
        except Exception as e:
            os.chdir(self.root_dir)
            raise Exception(e)

        _opt_end = time.time()
        print(f"\nTotal run time: {(_opt_end-_opt_start):.2f}")

    def post_process(
        self, plot_convergence: bool = True, theoretical_convergence: float = None
    ) -> pd.DataFrame:
        """Crawls through iteration directories to compile results."""
        iteration_dirs = [
            i
            for i in os.listdir(self.working_dir)
            if os.path.isdir(os.path.join(self.working_dir, i))
        ]

        results = []
        for directory in iteration_dirs:
            iteration = int(directory)

            completion_file = os.path.join(
                self.working_dir, directory, self.completion_filename
            )
            if os.path.exists(completion_file):
                # This iteration completed, load the results
                iter_result = pd.read_csv(completion_file)

                # Add iteration number
                iter_result.loc[len(iter_result)] = ["iteration", iteration]

                # Append
                results.append(iter_result.set_index("Unnamed: 0").to_dict()["0"])

        df = pd.DataFrame(results).set_index("iteration").sort_index()

        if plot_convergence:
            plt.plot(df.index, df["objective"], label="PySAGAS ShapeOpt convergence")
            plt.title("Convergence of Objective Function")
            plt.xlabel("Iteration")
            plt.ylabel("Objective Function")

            if theoretical_convergence:
                plt.axhline(
                    theoretical_convergence,
                    c="k",
                    ls="--",
                    label="Theoretical convergence",
                )

            plt.legend()
            plt.grid()
            plt.show()

        return df

    def _infer_adapt(self, sim_dir) -> str:
        with open(f"{sim_dir}/aero.csh", "r") as f:
            lines = f.readlines()

            for line in lines:
                if line.find("set n_adapt_cycles") != -1:
                    return f"adapt{int(line.split('=')[-1]):02d}"

    def _overwrite_adapt(self, sim_dir: str, max_adapt: int) -> str:
        """Overwrites the adapt cycle in the iteration directory."""
        original = os.path.join(sim_dir, "aero.csh")
        new = os.path.join(sim_dir, "temp_aero.csh")
        with open(new, "w+") as new_file:
            with open(original, "r") as original_file:
                for line in original_file:
                    if line.startswith("set n_adapt_cycles"):
                        # This is the line setting the adapt number
                        line = f"{line.split('=')[0]}= {max_adapt}"

                    # Write line to new file
                    new_file.write(line)

        # Replace original aero.csh file with updated file
        shutil.copymode(original, new)
        os.remove(original)
        os.rename(new, original)

    @staticmethod
    def _combine_sense_data(
        components_filepath: str,
        sensitivity_files: List[str],
        match_target: float = 0.9,
        tol_0: float = 1e-5,
        max_tol: float = 1e-1,
        outdir: Optional[str] = None,
    ):
        """Combine the component sensitivity data for intersected geometry."""
        match_frac = 0
        tol = tol_0
        while match_frac < match_target:
            # Check tolerance
            if tol > max_tol:
                raise Exception(
                    "Cannot combine sensitivity data (match fraction: "
                    + f"{match_frac}, tolerance: {tol}, max tolerance: {max_tol})."
                )

            # Run matching algorithm
            match_frac = append_sensitivities_to_tri(
                dp_filenames=sensitivity_files,
                components_filepath=components_filepath,
                match_tolerance=tol,
                verbosity=0,
                outdir=outdir,
            )

            if match_frac < match_target:
                print(
                    "Failed to combine sensitivity data "
                    f"({100*match_frac:.02f}% match rate)."
                )
                print("  Increasing matching tolerance and trying again.")

            # Increase matching tolerance
            tol *= 10

        print("Component sensitivity data combined successfully.")

    def _c3d_running(self) -> bool:
        with open(self.c3d_logname) as f:
            # Get last line in log file
            for line in f:
                pass

            # Check if if it is in the known errors
            for e in ShapeOpt.C3D_errors:
                if e in line:
                    return False, e

        # No errors
        return True, None

    def _copy_warmstart_files(
        self, warm_iter_dir: str, new_iter_dir: str
    ) -> Tuple[str, Dict[str, str]]:
        """Copies the files required to warm-start Cart3D."""
        warm_sim_dir = os.path.join(warm_iter_dir, self.sim_dir_name)
        new_sim_dir = os.path.join(new_iter_dir, self.sim_dir_name)
        warmstart_files = [
            "input.cntl",
            "input.c3d",
            "Config.xml",
        ]
        softlinks = [
            "BEST/Mesh.c3d.Info",
            "BEST/Mesh.mg.c3d",
        ]

        # Copy sim files
        for file in warmstart_files:
            shutil.copyfile(
                os.path.join(warm_sim_dir, file),
                os.path.join(new_sim_dir, file),
            )

        # Create soft links to Mesh files
        for file in softlinks:
            os.symlink(
                os.path.join(warm_sim_dir, file),
                os.path.join(new_sim_dir, f"ref{Path(file).name}"),
            )

        # Move *.tri files
        tri_files = glob.glob("*.tri")
        for file in tri_files:
            shutil.move(file, os.path.join(new_sim_dir, Path(file).name))

        # Also copy checkpoint file
        check_fp = glob.glob(os.path.join(warm_sim_dir, "BEST/FLOW/check.*"))[0]
        checkpoint_filename = Path(check_fp).name
        shutil.copyfile(
            check_fp,
            os.path.join(new_sim_dir, checkpoint_filename),
        )

        # Fetch run commands
        commands = {
            "cubes": self._find_in_file(
                os.path.join(warm_sim_dir, "BEST/Mesh.c3d.Info"), "====> cubes"
            ).split("====> ")[-1],
            "mgPrep": self._find_in_file(
                os.path.join(warm_sim_dir, "BEST/cart3d.out"), "mgPrep"
            ),
            "flowCart": self._find_in_file(
                os.path.join(warm_sim_dir, "BEST/FLOW/cart3d.out"), "flowCart"
            ),
        }
        return checkpoint_filename, commands

    @staticmethod
    def _find_in_file(filepath: str, match: str) -> str:
        """Find and return a line in a file by matching part of
        the line with a string."""
        with open(filepath, "r") as f:
            lines = f.readlines()
            for line in lines:
                if line.find(match) != -1:
                    # Matched line
                    return line

    @staticmethod
    def _get_last_iteration(working_dir: str) -> int:
        """Returns the number of last iteration performed."""
        if not os.path.exists(working_dir):
            # Working directory doesn't exist yet
            return 0

        # Collect iteration directories in working directory
        iteration_dirs = [
            int(i)
            for i in os.listdir(working_dir)
            if os.path.isdir(os.path.join(working_dir, i))
        ]
        return max(iteration_dirs) if iteration_dirs else 0

    @staticmethod
    def _body_to_aero(body_forces, aoa):
        """Converts the body forces into aero forces."""
        Cl_sens = body_forces["dFy/dp"] * np.cos(aoa) - body_forces["dFx/dp"] * np.sin(
            aoa
        )
        Cd_sens = body_forces["dFy/dp"] * np.sin(aoa) + body_forces["dFx/dp"] * np.cos(
            aoa
        )
        return Cl_sens, Cd_sens


class C3DPrep:
    def __init__(
        self,
        logfile,
        jitter_denom: float = 1000,
        rotation_attempts: int = 6,
        info_file: str = None,
    ) -> None:
        self._logfile = logfile
        self._info = info_file if info_file is not None else self._logfile
        self._jitter_denom = jitter_denom  # for 1000; Max of 0.0001, min of 0
        self._rotation_attempts = rotation_attempts

    def _run_stl2tri(self, stl_files: list):
        tri_files = []
        for file in stl_files:
            prefix = file.split(".")[0]
            tri_file = prefix + ".tri"
            os.system(f"stl2tri.pl {file} {tri_file} >> {self._logfile} 2>&1")
            tri_files.append(tri_file)

        os.system(f"rm *.comp.tri *.off >> {self._logfile} 2>&1")
        return tri_files

    def _jitter_tri_files(self, tri_files):
        for file in tri_files:
            prefix = file.split(".")[0]
            x_pert = random() / self._jitter_denom
            y_pert = random() / self._jitter_denom
            z_pert = random() / self._jitter_denom
            os.system(
                f"trix -x {x_pert} -y {y_pert} -z {z_pert} -o {prefix} {file} >> {self._logfile} 2>&1"
            )

    def _shift_all(
        self,
        tri_files,
        x_shift,
        y_shift,
        z_shift,
        component: str = None,
        reverse: bool = False,
    ):
        if component is None:
            transform_files = tri_files
        else:
            transform_files = [component]

        for file in transform_files:
            prefix = ".".join(file.split(".")[:-1])
            if reverse:
                os.system(
                    f"trix -x {-x_shift} -y {-y_shift} -z {-z_shift} -o {prefix} {file} >> {self._logfile} 2>&1"
                )
            else:
                os.system(
                    f"trix -x {x_shift} -y {y_shift} -z {z_shift} -o {prefix} {file} >> {self._logfile} 2>&1"
                )

    def _rotate_all(
        self,
        tri_files,
        x_rot,
        y_rot,
        z_rot,
        component: str = None,
        reverse: bool = False,
    ):
        # Define rotations dict
        rotations = {
            "x": x_rot,
            "y": y_rot,
            "z": z_rot,
        }

        # Determine files to be transformed
        if component is None:
            # Rotate all .tri files (as provided)
            transform_files = tri_files
        else:
            # Rotate the single component
            transform_files = [component]

        for file in transform_files:
            prefix = ".".join(file.split(".")[:-1])

            # Check order of operations
            if reverse:
                order = ["z", "y", "x"]
            else:
                order = ["x", "y", "z"]

            # Apply rotations
            for axis in order:
                rotation = rotations[axis]
                os.system(
                    f"trix -r{axis} {rotation} -o {prefix} {file} >> {self._logfile} 2>&1"
                )

    def _run_comp2tri(self, tri_files):
        tri_files_str = " ".join(tri_files)

        if os.path.exists("Config.xml"):
            # Remove old config file
            os.remove("Config.xml")

        # Run command
        os.system(
            f"comp2tri -makeGMPtags {tri_files_str} -config >> {self._logfile} 2>&1"
        )

        # Await Config.xml
        while not os.path.exists("Config.xml"):
            time.sleep(0.2)

        # Overwrite Config.xml Component names using tri files
        original = os.path.join("Config.xml")
        new = os.path.join("temp_config.xml")
        with open(new, "w+") as new_file:
            with open(original, "r") as original_file:
                for line in original_file:
                    if "Component Name" in line:
                        # Get component number
                        name_prefix = "Component_"
                        name_start = line.index(name_prefix)
                        comp_no = line.split('"')[1].split("_")[-1]
                        tri_prefix = tri_files[int(comp_no) - 1].split(".")[0]

                        # Update line
                        line = (
                            line[:name_start]
                            + tri_prefix
                            + line[name_start + len(name_prefix) + 1 :]
                        )

                    # Write line to new file
                    new_file.write(line)

        # # Replace original aero.csh file with updated file
        shutil.copymode(original, new)
        os.remove(original)
        os.rename(new, original)

    def _run_intersect(self):
        os.system(f"intersect >> {self._logfile} 2>&1")

    @staticmethod
    def _get_stl_files():
        path = os.getcwd()
        all_files = [
            f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))
        ]
        stl_files = []
        for file in all_files:
            if file.split(".")[-1] == "stl":
                stl_files.append(file)

        # Sort files
        stl_files.sort()

        return stl_files

    @staticmethod
    def _check_for_success():
        success = False
        if os.path.isfile("Components.i.tri"):
            success = True
        return success

    def intersect_stls(self) -> bool:
        """Create Components.i.tri by intersecting all STL files."""
        # Check for existing intersected file
        if self._check_for_success():
            self._log("Intersected components file already present.")
            return True

        # Continue
        stl_files = self._get_stl_files()
        tri_files = self._run_stl2tri(stl_files)

        # First try intersect original files
        self._log("Making first attempt to intersect components with no perturbations.")
        self._run_comp2tri(tri_files)
        self._run_intersect()
        successful = self._check_for_success()
        if successful:
            self._log("Success.")
            return True

        # That failed, try jittering components
        self._log("Attempt failed, now attempting to intersect jittered components.")
        self._jitter_tri_files(tri_files)
        self._run_comp2tri(tri_files)
        self._run_intersect()
        successful = self._check_for_success()
        if successful:
            self._log("Success.")
            return True

        # That failed, try arbitrary shifts away
        self._log("Attempt failed, now attempting random perturbations.")
        for attempt in range(self._rotation_attempts):
            # Define shifts
            x_shift = random() * 10  # Max of 10, min of 0
            y_shift = random() * 10  # Max of 10, min of 0
            z_shift = random() * 10  # Max of 10, min of 0

            # Define rotations
            x_rot = random() * 10  # Max of 10, min of 0
            y_rot = random() * 10  # Max of 10, min of 0
            z_rot = random() * 10  # Max of 10, min of 0

            # Apply transformations
            self._shift_all(
                tri_files=tri_files, x_shift=x_shift, y_shift=y_shift, z_shift=z_shift
            )
            self._rotate_all(tri_files=tri_files, x_rot=x_rot, y_rot=y_rot, z_rot=z_rot)

            if attempt > 0:
                # Also jitter
                self._log(f"On attempt {attempt+1}, also applying random jitter.")
                self._jitter_tri_files(tri_files)

            # Make intersect attempt
            self._run_comp2tri(tri_files)
            self._run_intersect()
            successful = self._check_for_success()

            if successful:
                # Move configuration back to original location
                self._log(
                    "Success. Now moving Components.i.tri back to original position."
                )
                self._rotate_all(
                    tri_files=tri_files,
                    x_rot=-x_rot,
                    y_rot=-y_rot,
                    z_rot=-z_rot,
                    component="Components.i.tri",
                    reverse=True,  # Perform in reverse order
                )
                self._shift_all(
                    tri_files=tri_files,
                    x_shift=x_shift,
                    y_shift=y_shift,
                    z_shift=z_shift,
                    component="Components.i.tri",
                    reverse=True,
                )
                return True

            else:
                # Need to reset tri files
                self._log(
                    f"Random perturbation attempt {attempt+1} failed. Resetting "
                    + ".tri files and attempting again."
                )
                tri_files = self._run_stl2tri(stl_files)

        # Finish log
        self._log("Unsuccessful.")

        return False

    def run_autoinputs(self):
        """Runs autoInputs to create input.c3d."""
        os.system(f"autoInputs -r 2 >> {self._logfile} 2>&1")

    def _log(self, msg: str):
        with open(self._info, "a") as f:
            f.write("\nC3DPrep: " + msg)
