import numpy as np
import rospy
from collections import OrderedDict
import gym
from gym.spaces import Box
from sawyer_control.pd_controllers.joint_angle_pd_controller import AnglePDController
from sawyer_control.core.eval_util import create_stats_ordered_dict
from sawyer_control.core.serializable import Serializable
from sawyer_control.core.multitask_env import MultitaskEnv
from sawyer_control.configs import *
from sawyer_control.srv import observation
from sawyer_control.srv import getRobotPoseAndJacobian
from sawyer_control.srv import ik
from sawyer_control.srv import angle_action
from sawyer_control.msg import actions
'''
TODOs:
safety box configs 
'''
class SawyerEnv(gym.Env, Serializable, MultitaskEnv):
    def __init__(
            self,
            action_mode='torque',
            use_safety_box=True,
            torque_action_scale=1,
            position_action_scale=1/10,
            config = base_config,
    ):
        Serializable.quick_init(self, locals())
        self.config = config
        self.init_rospy(self.config.UPDATE_HZ)
        self.action_mode = action_mode
        self.safety_box_lows = self.not_reset_safety_box_lows = [0.1, -0.5, 0]
        self.safety_box_highs = self.not_reset_safety_box_highs = [0.7, 0.5, 0.7]
        if action_mode == 'position':
            # self.ee_safety_box_low = np.array([0.23, -.302, 0.03])
            # self.ee_safety_box_high = np.array([0.60, .47, 0.409])
            self.ee_safety_box_low = np.array([.2, -.2, .03])
            self.ee_safety_box_high = np.array([.6, .2, .5])
            # original ee safety box
            #self.ee_safety_box_high = np.array([0.73, 0.32, 0.4])
            #self.ee_safety_box_low = np.array([0.52, 0.03, 0.05])

        # image box
        # self.safety_box_lows = self.not_reset_safety_box_lows = [.2, -.332, .00]
        # self.safety_box_highs = self.not_reset_safety_box_highs = [.63, .5, .429]
        # self.safety_box_lows = self.not_reset_safety_box_lows = [.2, -.2, .00]
        # self.safety_box_highs = self.not_reset_safety_box_highs = [.6, .2, .5]

        self.use_safety_box = use_safety_box
        self.AnglePDController = AnglePDController(config=self.config)

        self._set_action_space()
        self._set_observation_space()
        self.get_latest_pose_jacobian_dict()
        self.torque_action_scale = torque_action_scale
        self.position_action_scale = position_action_scale
        self.in_reset = True

    def _act(self, action):
        if self.action_mode == 'position':
            self._position_act(action * self.position_action_scale)
        else:
            self._torque_act(action*self.torque_action_scale)
        return

    def _position_act(self, action):
        ee_pos = self._get_endeffector_pose()
        endeffector_pos = ee_pos[:3]
        endeffector_angles = ee_pos[3:]
        target_ee_pos = (endeffector_pos + action)
        target_ee_pos = np.clip(target_ee_pos, self.ee_safety_box_low, self.ee_safety_box_high)
        target_ee_pos = np.concatenate((target_ee_pos, endeffector_angles))
        angles = self.request_ik_angles(target_ee_pos, self._get_joint_angles())
        self.send_angle_action(angles)

    def _torque_act(self, action):
        if self.use_safety_box:
            if self.in_reset:
                self.safety_box_highs = self.config.RESET_SAFETY_BOX_HIGHS
                self.safety_box_lows = self.config.RESET_SAFETY_BOX_LOWS
            else:
                self.safety_box_lows = self.not_reset_safety_box_lows
                self.safety_box_highs = self.not_reset_safety_box_highs
            self.get_latest_pose_jacobian_dict()
            truncated_dict = self.check_joints_in_box()
            if len(truncated_dict) > 0:
                forces_dict = self._get_adjustment_forces_per_joint_dict(truncated_dict)
                torques = np.zeros(7)
                for joint in forces_dict:
                    jacobian = truncated_dict[joint][1]
                    force = forces_dict[joint]
                    torques = torques + np.dot(jacobian.T, force).T
                torques[-1] = 0
                action = torques
        if self.in_reset:
            action = np.clip(action, self.config.RESET_TORQUE_LOW, self.config.RESET_TORQUE_HIGH)
        else:
            action = np.clip(np.asarray(action), self.config.JOINT_TORQUE_LOW, self.config.JOINT_TORQUE_HIGH)
        self.send_action(action)
        self.rate.sleep()

    def _wrap_angles(self, angles):
        return angles % (2*np.pi)

    def _get_joint_angles(self):
        angles, _, _, _ = self.request_observation()
        return angles

    def _get_endeffector_pose(self):
        _, _, _, endpoint_pose = self.request_observation()
        return endpoint_pose

    def compute_angle_difference(self, angles1, angles2):
        deltas = np.abs(angles1 - angles2)
        differences = np.minimum(2 * np.pi - deltas, deltas)
        return differences

    def step(self, action):
        self._act(action)
        observation = self._get_obs()
        reward = self.compute_rewards(action, observation, self._state_goal)
        info = self._get_info()
        done = False
        return observation, reward, done, info

    def _get_info(self):
        return dict()

    def _get_obs(self):
        angles, velocities, _, endpoint_pose = self.request_observation()
        obs = np.hstack((
            angles,
            velocities,
            endpoint_pose,
        ))
        return obs

    def _safe_move_to_neutral(self):
        for i in range(self.config.RESET_LENGTH):
            cur_pos, cur_vel, _, _ = self.request_observation()
            torques = self.AnglePDController._compute_pd_forces(cur_pos, cur_vel)
            self._torque_act(torques)
            if self._reset_complete():
                break

    def _reset_complete(self):
        close_to_desired_reset_pos = self._check_reset_angles_within_threshold()
        _, velocities, _, _ = self.request_observation()
        velocities = np.abs(np.array(velocities))
        VELOCITY_THRESHOLD = .002 * np.ones(7)
        no_velocity = (velocities < VELOCITY_THRESHOLD).all()
        return close_to_desired_reset_pos and no_velocity
    
    def _check_reset_angles_within_threshold(self):
        desired_neutral = self.AnglePDController._des_angles
        desired_neutral = np.array([desired_neutral[joint] for joint in self.config.JOINT_NAMES])
        actual_neutral = (self._get_joint_angles())
        errors = self.compute_angle_difference(desired_neutral, actual_neutral)
        is_within_threshold = (errors < self.config.RESET_ERROR_THRESHOLD).all()
        return is_within_threshold

    def reset(self):
        self.in_reset = True
        self._safe_move_to_neutral()
        self.in_reset = False
        return self._get_obs()

    def get_latest_pose_jacobian_dict(self):
        self.pose_jacobian_dict = self._get_robot_pose_jacobian_client('right') #why do we need to pass in 'right', why not just hardcode it to be right since that will never change

    def _get_robot_pose_jacobian_client(self, name):
        rospy.wait_for_service('get_robot_pose_jacobian')
        try:
            get_robot_pose_jacobian = rospy.ServiceProxy('get_robot_pose_jacobian', getRobotPoseAndJacobian,
                                                         persistent=True)
            resp = get_robot_pose_jacobian(name)
            pose_jac_dict = self._unpack_pose_jacobian_dict(resp.poses, resp.jacobians)
            return pose_jac_dict
        except rospy.ServiceException as e:
            print(e)

    def _unpack_pose_jacobian_dict(self, poses, jacobians):
        pose_jacobian_dict = {}
        pose_counter = 0
        jac_counter = 0
        poses = np.array(poses)
        jacobians = np.array(jacobians)
        for link in self.config.LINK_NAMES:
            pose = poses[pose_counter:pose_counter + 3]
            jacobian = []
            for i in range(jac_counter, jac_counter+21, 7):
                jacobian.append(jacobians[i:i+7])
            jacobian = np.array(jacobian)
            pose_counter += 3
            jac_counter += 21
            pose_jacobian_dict[link] = [pose, jacobian]
        return pose_jacobian_dict

    def _get_positions_from_pose_jacobian_dict(self):
        poses = []
        for joint in self.pose_jacobian_dict.keys():
            poses.append(self.pose_jacobian_dict[joint][0])
        return np.array(poses)

    def check_joints_in_box(self):
        joint_dict = self.pose_jacobian_dict.copy()
        keys_to_remove = []
        for joint in joint_dict.keys():
            if self._pose_in_box(joint_dict[joint][0]):
                keys_to_remove.append(joint)
        for key in keys_to_remove:
            del joint_dict[key]
        return joint_dict

    def _pose_in_box(self, pose):
        #TODO: DOUBLE CHECK THIS WORKS
        within_box = self.safety_box.contains(pose)
        return within_box

    def _get_adjustment_forces_per_joint_dict(self, joint_dict):
        forces_dict = {}
        for joint in joint_dict:
            force = self._get_adjustment_force_from_pose(joint_dict[joint][0])
            forces_dict[joint] = force
        return forces_dict

    def _get_adjustment_force_from_pose(self, pose):
        x, y, z = 0, 0, 0

        curr_x = pose[0]
        curr_y = pose[1]
        curr_z = pose[2]

        if curr_x > self.safety_box_highs[0]:
            x = -1 * np.exp(np.abs(curr_x - self.safety_box_highs[0]) * self.config.SAFETY_FORCE_TEMPERATURE) * self.config.SAFETY_FORCE_MAGNITUDE
        elif curr_x < self.safety_box_lows[0]:
            x = np.exp(np.abs(curr_x - self.safety_box_lows[0]) * self.config.SAFETY_FORCE_TEMPERATURE) * self.config.SAFETY_FORCE_MAGNITUDE

        if curr_y > self.safety_box_highs[1]:
            y = -1 * np.exp(np.abs(curr_y - self.safety_box_highs[1]) * self.config.SAFETY_FORCE_TEMPERATURE) * self.config.SAFETY_FORCE_MAGNITUDE
        elif curr_y < self.safety_box_lows[1]:
            y = np.exp(np.abs(curr_y - self.safety_box_lows[1]) * self.config.SAFETY_FORCE_TEMPERATURE) * self.config.SAFETY_FORCE_MAGNITUDE

        if curr_z > self.safety_box_highs[2]:
            z = -1 * np.exp(np.abs(curr_z - self.safety_box_highs[2]) * self.config.SAFETY_FORCE_TEMPERATURE) * self.config.SAFETY_FORCE_MAGNITUDE
        elif curr_z < self.safety_box_lows[2]:
            z = np.exp(np.abs(curr_z - self.safety_box_highs[2]) * self.config.SAFETY_FORCE_TEMPERATURE) * self.config.SAFETY_FORCE_MAGNITUDE
        return np.array([x, y, z])

    def _compute_joint_distance_outside_box(self, pose):
        curr_x = pose[0]
        curr_y = pose[1]
        curr_z = pose[2]
        if(self._pose_in_box(pose)):
            x, y, z = 0, 0, 0
        else:
            x, y, z = 0, 0, 0
            if curr_x > self.safety_box_highs[0]:
                x = np.abs(curr_x - self.safety_box_highs[0])
            elif curr_x < self.safety_box_lows[0]:
                x = np.abs(curr_x - self.safety_box_lows[0])
            if curr_y > self.safety_box_highs[1]:
                y = np.abs(curr_y - self.safety_box_highs[1])
            elif curr_y < self.safety_box_lows[1]:
                y = np.abs(curr_y - self.safety_box_lows[1])
            if curr_z > self.safety_box_highs[2]:
                z = np.abs(curr_z - self.safety_box_highs[2])
            elif curr_z < self.safety_box_lows[2]:
                z = np.abs(curr_z - self.safety_box_lows[2])
        return np.linalg.norm([x, y, z])

    def get_diagnostics(self, paths, prefix=''):
        raise NotImplementedError()

    @property
    def action_space(self):
        return self._action_space

    @property
    def observation_space(self):
        return self._observation_space
    
    def _set_action_space(self):
        if self.action_mode == 'position':
            self._action_space = Box(
                self.config.POSITION_CONTROL_LOW,
                self.config.POSITION_CONTROL_HIGH,
            )
        else:
            self._action_space = Box(
                self.config.JOINT_TORQUE_LOW,
                self.config.JOINT_TORQUE_HIGH
            )

    def _set_observation_space(self):
        lows = np.hstack((
            self.config.JOINT_VALUE_LOW['position'],
            self.config.JOINT_VALUE_LOW['velocity'],
            self.config.END_EFFECTOR_VALUE_LOW['position'],
            self.config.END_EFFECTOR_VALUE_LOW['angle'],
        ))
        highs = np.hstack((
            self.config.JOINT_VALUE_HIGH['position'],
            self.config.JOINT_VALUE_HIGH['velocity'],
            self.config.END_EFFECTOR_VALUE_HIGH['position'],
            self.config.END_EFFECTOR_VALUE_HIGH['angle'],
        ))
        self._observation_space = Box(
            lows,
            highs,
        )
            
    """ 
    ROS Functions 
    """

    def init_rospy(self, update_hz):
        rospy.init_node('sawyer_env', anonymous=True)
        self.action_publisher = rospy.Publisher('actions_publisher', actions, queue_size=10)
        self.rate = rospy.Rate(update_hz)

    def send_action(self, action):
        self.action_publisher.publish(action)

    def send_angle_action(self, action):
        self.request_angle_action(action)

    def request_observation(self):
        rospy.wait_for_service('observations')
        try:
            request = rospy.ServiceProxy('observations', observation, persistent=True)
            obs = request()
            #TODO: FIX THIS TO RETURN NP ARRAYS ONLY
            return (
                    self._wrap_angles(np.array(obs.angles)),
                    np.array(obs.velocities),
                    np.array(obs.torques),
                    np.array(obs.endpoint_pose)
            )
        except rospy.ServiceException as e:
            print(e)

    def request_angle_action(self, angles):
        rospy.wait_for_service('angle_action')
        try:
            execute_action = rospy.ServiceProxy('angle_action', angle_action, persistent=True)
            execute_action(angles)
            return None
        except rospy.ServiceException as e:
            print(e)


    def request_ik_angles(self, ee_pos, joint_angles):
        rospy.wait_for_service('ik')
        try:
            get_joint_angles = rospy.ServiceProxy('ik', ik, persistent=True)
            resp = get_joint_angles(ee_pos, joint_angles)

            return (
                resp.joint_angles
            ) #TODO: why is this in a tuple? can this be removed?
        except rospy.ServiceException as e:
            print(e)

    """
    Multitask functions
    """

    @property
    def goal_dim(self):
        return 3

    def get_goal(self):
        return self._state_goal

    def sample_goals(self, batch_size):
        if self.fix_goal:
            goals = np.repeat(
                self.fixed_goal.copy()[None],
                batch_size,
                0
            )
        else:
            goals = np.random.uniform(
                self.goal_space.low,
                self.goal_space.high,
                size=(batch_size, self.goal_space.low.size),
            )
        return goals

    def compute_rewards(self, actions, obs, goals):
        distances = np.linalg.norm(obs - goals, axis=1)
        if self.reward_type == 'hand_distance':
            r = -distances
        elif self.reward_type == 'hand_success':
            r = -(distances < self.indicator_threshold).astype(float)
        else:
            raise NotImplementedError("Invalid/no reward type.")
        return r

    def set_to_goal(self, goal):
        raise NotImplementedError()

    def get_env_state(self):
        raise NotImplementedError()

    def set_env_state(self, state):
        raise NotImplementedError()