import shutil
import subprocess
import os

from pippin.config import mkdirs, get_output_loc, get_config
from pippin.task import Task


class CosmoMC(Task):  # TODO: Define the location of the output so we can run the lc fitting on it.
    """ Smack the data into something that looks like the simulated data


    """
    def __init__(self, name, output_dir, options, dependencies=None):
        super().__init__(name, output_dir, dependencies=dependencies)
        self.options = options
        self.global_config = get_config()

        self.job_name = f"cosmomc_{name}"
        self.logfile = os.path.join(self.output_dir, "output.log")

        self.path_to_cosmomc = self.global_config["CosmoMC"]["location"]

        self.slurm = """#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --time=12:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=4
#SBATCH --cpus-per-task=1
#SBATCH --partition=broadwl
#SBATCH --output={log_file}
#SBATCH --account=pi-rkessler
#SBATCH --mem=10GB

export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK

mpirun {path_to_cosmomc} {path_to_ini}

if [ $? -eq 0 ]; then
    echo "SUCCESS" > {done_file}
else
    echo "FAILURE" > {done_file}
fi
"""

    def _check_completion(self, squeue):
        if os.path.exists(self.done_file):
            self.logger.debug(f"Done file found at f{self.done_file}")
            with open(self.done_file) as f:
                if "FAILURE" in f.read():
                    self.logger.info(f"Done file reported failure. Check output log {self.logfile}")
                    return Task.FINISHED_FAILURE
                else:
                    return Task.FINISHED_SUCCESS
        return 4

    def get_ini_file(self):
        pass

    def _run(self, force_refresh):

        ini_file = self.get_ini_file()

        format_dict = {
            "job_name": self.job_name,
            "log_file": self.logfile,
            "done_file": self.done_file,
            "path_to_cosmomc": self.path_to_cosmomc
        }
        final_slurm = self.slurm.format(**format_dict)

        new_hash = self.get_hash_from_string(final_slurm)
        old_hash = self.get_old_hash()

        if force_refresh or new_hash != old_hash:
            self.logger.debug("Regenerating and launching task")
            shutil.rmtree(self.output_dir, ignore_errors=True)
            mkdirs(self.output_dir)
            self.save_new_hash(new_hash)
            slurm_output_file = os.path.join(self.output_dir, "slurm.job")
            with open(slurm_output_file, "w") as f:
                f.write(final_slurm)

            self.logger.info(f"Submitting batch job for data prep")
            subprocess.run(["sbatch", slurm_output_file], cwd=self.output_dir)
        else:
            self.logger.info("Hash check passed, not rerunning")
        return True