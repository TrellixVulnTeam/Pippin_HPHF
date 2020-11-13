import inspect
import shutil
import subprocess
import os
from pathlib import Path

import yaml

from pippin.base import ConfigBasedExecutable
from pippin.biascor import BiasCor
from pippin.config import mkdirs, get_config, get_data_loc, read_yaml
from pippin.task import Task


class CreateCov(ConfigBasedExecutable):
    """ Create covariance matrices and data from salt2mu used for cosmomc

    CONFIGURATION:
    ==============
    CREATE_COV:
        label:
            OPTS:
              SUBTRACT_VPEC: False # Subtract VPEC contribution from MUERR if True
              SYS_SCALE: location of the fitopts file with scales in it
              FITOPT_SCALES:  # Optional dict to scale fitopts
                    fitopt_label_for_partial check: float to scale by  # (does label in fitopt, not exact match
              MUOPT_SCALES: # Optional dict used to construct SYSFILE input by putting MUOPT scales at the bottom, scale defaults to one
                exact_muopt_name: float
              COVOPTS:  # optional, note you'll get an 'ALL' covopt no matter what
                - "[NOSYS] [=DEFAULT,=DEFAULT]"  # syntax for Dan&Dillons script. [label] [fitopts_to_match,muopts_to_match]. Does partial matching. =Default means dont do that systematic type

    OUTPUTS:
    ========
        name : name given in the yml
        output_dir: top level output directory
        ini_dir : The directory the .ini files for cosmomc will be output to
        covopts : a dictionary mapping a covopt label to a number
        blind: bool - whether or not to blind cosmo results

    """

    def __init__(self, name, output_dir, config, options, global_config, dependencies=None, index=0):

        base_file = get_data_loc("create_cov/input_file.txt")
        super().__init__(name, output_dir, config, base_file, default_assignment=": ", dependencies=dependencies)

        if options is None:
            options = {}
        self.options = options
        self.templates_dir = self.options.get("INI_DIR", "cosmomc_templates")
        self.global_config = get_config()
        self.index = index
        self.job_name = os.path.basename(Path(output_dir).parents[1]) + "_CREATE_COV_" + name
        self.path_to_code = os.path.abspath(os.path.dirname(inspect.stack()[0][1]) + "/external")

        self.logfile = os.path.join(self.output_dir, "output.log")
        self.sys_file_out = os.path.join(self.output_dir, "sys_scale.yml")
        self.chain_dir = os.path.join(self.output_dir, "chains/")
        self.config_dir = os.path.join(self.output_dir, "output")
        self.subtract_vpec = options.get("SUBTRACT_VPEC", False)
        self.binned = options.get("BINNED", not self.subtract_vpec)

        self.biascor_dep = self.get_dep(BiasCor, fail=True)
        self.sys_file_in = self.get_sys_file_in()
        self.output["blind"] = self.biascor_dep.output["blind"]
        self.input_file = os.path.join(self.output_dir, self.biascor_dep.output["subdirs"][index] + ".input")
        self.output["hubble_plot"] = self.biascor_dep.output["hubble_plot"]

        self.output["ini_dir"] = os.path.join(self.config_dir, "cosmomc")
        covopts_map = {"ALL": 0}
        for i, covopt in enumerate(self.options.get("COVOPTS", [])):
            covopts_map[covopt.split("]")[0][1:]] = i + 1
        self.output["covopts"] = covopts_map
        self.output["index"] = index
        self.output["bcor_name"] = self.biascor_dep.name
        self.slurm = """#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --time=00:10:00
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --partition=broadwl
#SBATCH --output={log_file}
#SBATCH --account=pi-rkessler
#SBATCH --mem=4GB

cd {output_dir}
source activate
python {path_to_code}/create_covariance.py {unbinned} {subtract_vpec} {input_file}
if [ $? -eq 0 ]; then
    echo SUCCESS > {done_file}
else
    echo FAILURE > {done_file}
fi
"""

    def get_sys_file_in(self):
        set_file = self.options.get("SYS_SCALE")
        if set_file is not None:
            self.logger.debug(f"Explicit SYS_SCALE file specified: {set_file}")
            path = get_data_loc(set_file)
            if path is None:
                raise ValueError(f"Unable to resolve path to {set_file}")
        else:
            self.logger.debug("Searching for SYS_SCALE source from biascor task")
            fitopt_files = [f for f in self.biascor_dep.output["fitopt_files"] if f is not None]
            assert len(set(fitopt_files)) < 2, f"Cannot automatically determine scaling from FITOPT file as you have multiple files: {fitopt_files}"
            if fitopt_files:
                path = fitopt_files[0]
            else:
                path = None
        self.options["SYS_SCALE"] = path  # Save to options so its serialised out
        self.logger.info(f"Setting systematics scaling file to {path}")
        return path

    def _check_completion(self, squeue):
        if os.path.exists(self.done_file):
            self.logger.debug(f"Done file found at {self.done_file}")
            with open(self.done_file) as f:
                if "FAILURE" in f.read():
                    self.logger.error(f"Done file reported failure. Check output log {self.logfile}")
                    self.scan_files_for_error([self.logfile], "ERROR", "EXCEPTION")
                    return Task.FINISHED_FAILURE
                else:
                    return Task.FINISHED_SUCCESS
        return self.check_for_job(squeue, self.job_name)

    def get_scales_from_fitopt_file(self):
        if self.sys_file_in is None:
            return {}
        self.logger.debug(f"Loading sys scaling from {self.sys_file_in}")
        yaml = read_yaml(self.sys_file_in)
        raw = {k: float(v.split(maxsplit=1)[0]) for _, d in yaml.items() for k, v in d.items()}
        return raw

    def calculate_input(self):
        self.logger.debug(f"Calculating input")
        self.yaml["COSMOMC_TEMPLATES"] = get_data_loc(self.templates_dir)
        self.yaml["NAME"] = self.name
        self.yaml["SYSFILE"] = self.sys_file_out
        self.yaml["INPUT_DIR"] = self.biascor_dep.output["fit_output_dir"]
        self.yaml["OUTDIR"] = self.config_dir
        self.yaml["VERSION"] = self.biascor_dep.output["subdirs"][self.index]
        self.yaml["MUOPT_SCALES"] = self.biascor_dep.output["muopt_scales"]
        self.yaml["COVOPTS"] = self.options.get("COVOPTS", [])

        # Load in sys file, add muopt arguments if needed
        # Get the MUOPT_SCALES and FITOPT scales keywords
        sys_scale = {**self.get_scales_from_fitopt_file(), **self.options.get("FITOPT_SCALES", {})}
        return sys_scale

    def _run(self, force_refresh):
        sys_scale = self.calculate_input()
        format_dict = {
            "job_name": self.job_name,
            "log_file": self.logfile,
            "done_file": self.done_file,
            "path_to_code": self.path_to_code,
            "input_file": self.input_file,
            "output_dir": self.output_dir,
            "unbinned": "" if self.binned else "-u",
            "subtract_vpec": "" if not self.subtract_vpec else "-s",
        }
        final_slurm = self.slurm.format(**format_dict)

        final_output_for_hash = self.get_output_string() + yaml.safe_dump(sys_scale, width=2048) + final_slurm

        new_hash = self.get_hash_from_string(final_output_for_hash)
        old_hash = self.get_old_hash()

        if force_refresh or new_hash != old_hash:
            self.logger.debug("Regenerating and launching task")
            shutil.rmtree(self.output_dir, ignore_errors=True)
            mkdirs(self.output_dir)
            mkdirs(self.config_dir)
            self.save_new_hash(new_hash)
            # Write sys scales and the main input file
            with open(self.sys_file_out, "w") as f:
                f.write(yaml.safe_dump(sys_scale, width=2048))

            with open(self.input_file, "w") as f:
                f.write(self.get_output_string())
            # Write out slurm job script
            slurm_output_file = os.path.join(self.output_dir, "slurm.job")
            with open(slurm_output_file, "w") as f:
                f.write(final_slurm)

            self.logger.info(f"Submitting batch job for data prep")
            subprocess.run(["sbatch", slurm_output_file], cwd=self.output_dir)
        else:
            self.should_be_done()
            self.logger.info("Hash check passed, not rerunning")
        return True

    @staticmethod
    def get_tasks(c, prior_tasks, base_output_dir, stage_number, prefix, global_config):

        biascor_tasks = Task.get_task_of_type(prior_tasks, BiasCor)

        def _get_createcov_dir(base_output_dir, stage_number, name):
            return f"{base_output_dir}/{stage_number}_CREATE_COV/{name}"

        tasks = []
        for cname in c.get("CREATE_COV", []):
            config = c["CREATE_COV"][cname]
            if config is None:
                config = {}
            options = config.get("OPTS", {})
            mask = config.get("MASK", config.get("MASK_BIASCOR", ""))

            for btask in biascor_tasks:
                if mask not in btask.name:
                    continue

                num = len(btask.output["subdirs"])
                for i in range(num):
                    ii = "" if num == 1 else f"_{i + 1}"

                    name = f"{cname}_{btask.name}{ii}"
                    a = CreateCov(name, _get_createcov_dir(base_output_dir, stage_number, name), config, options, global_config, dependencies=[btask], index=i)
                    Task.logger.info(f"Creating createcov task {name} for {btask.name} with {a.num_jobs} jobs")
                    tasks.append(a)

            if len(biascor_tasks) == 0:
                Task.fail_config(f"Create cov task {cname} has no biascor task to run on!")

        return tasks
