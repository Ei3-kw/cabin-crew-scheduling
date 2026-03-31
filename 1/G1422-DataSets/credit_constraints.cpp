//============================================================================
// Name        : credit_constrains.cpp
// Author      : Frederic Quesnel
// Version     : 1.0
// Description : Generate credit constrains by base for multiple instances at once.
//				 The constrains limit the number of credited hours per base.
//               One of the base gets most of the credit, and the rest is
// 				 evenly distributed among the other bases. Different scenarios are
//			     displayed with a different percentage of credit assigned to the
//				 main base. Some slack is also added to the total number of credit
//				 ensure feasibility.
//============================================================================

#include <iostream>
#include <iomanip>
#include <cstdlib>
#include <vector>
#include <sstream>
#include <fstream>

using std::cout;
using std::cin;
using std::ios;
using std::endl;
using std::string;
using std::vector;



//selects base Names from file "listOfBases"
bool baseName(string, vector<string>*);
void calculateBriefingCredit(string,vector<string>, vector<int>*);



int main(int argc, char* argv[]) {

	//extracts parameters

	string a;//dummy variable used to discard useless input

	//--------------------------------------
	//extracts arguments from parameter file

	//name of parameter file
	std::stringstream paramsName(argv[1]);
	//open parameter file
	std::ifstream params(paramsName.str().c_str());
	string paramLine ;

	//list of all instance paths
	vector<string> instanceNames;

	//skip lines until it arrives at the "instances" line
	while(getline(params,paramLine) && !paramLine.compare("instances")==0){
	}

	//extract instance names, until an empty line is reached
	while(getline(params,paramLine) && !paramLine.compare("")==0){
		instanceNames.push_back(paramLine);
	}
	//skip lines until it arrives at the "credit_constrains.cpp" line
	while(getline(params,paramLine) && !paramLine.compare("credit_constrains.cpp")==0){
	}


	//Extracts the percentage of total slack credits
	float percentSlack ;
	params >>a>>a>>a>>percentSlack;
	
	//percentage of credited hours given to an employee
	float percentage;
	//extracts the percetages
	params >> a>>a>>a>>a>>a>>percentage;
	getline(params,paramLine);




	//number of instance
	int instanceNb = 0;


	//-------------------------------------------------
	//generate credit constrains for each instance

	while(instanceNb < (int) instanceNames.size()){

		std::cout<<instanceNames[instanceNb]<<std::endl;
		//list of the names of the bases
		vector<string> baseNames;

		//extracts the base names from the file listOfBases.csv, and if the program fails, skip the instance and display an error message
		if(!baseName(instanceNames[instanceNb],&baseNames)){
			cout<<"Error : listOfBases.csv file not found in instance "<<instanceNames[instanceNb]<<" . Proceeding with next instance."<<endl;
			break;
		}
		else{


			//---------------------------------------
			//count the initial credit for each base

			//list of credit for each base in a previous solution
			vector<float> initialCreditPerBase;
			//credit for all bases (sum of initialCreditPerBase
			float totalCredit = 0;

			//instanciate the vector initialCreditPerBase
			for(int i = 0 ; i < (int) baseNames.size() ; i++){
				initialCreditPerBase.push_back(0);
			}

			//open "creditedHours" file
			std::stringstream creditedHoursFileName;//name of file
			creditedHoursFileName << instanceNames[instanceNb] << "/creditedHours";
			std::ifstream creditedHoursFile(creditedHoursFileName.str().c_str(), ios::in);

			//if the program fails to open "creditedHours", display an error message and proceed with next instance
			if(creditedHoursFile.fail()){
				cout<< "Error : creditedHours file not found in instance "<<instanceNames[instanceNb]<<" . Proceeding with the next instance."<<endl;
			}
			//"creditedHours" file is open
			else{

				//string representing the name of a base, preceded by a number and surrounded by parentheses (ex. 2(BASE1) )
				string baseString;

			//read the file until the end is reached
			//each iteration correspond to the credit of information concerning one crew member
				while(creditedHoursFile >> a >> baseString >> a){
					int baseIndex = -1;
					//find the index of the right base
					for(int i = 0 ; i < (int) baseNames.size() ; i++){
						//if base name is found in baseString
						if( baseString.find(baseNames[i]) != string::npos){
							baseIndex = i;
							break;
						}
					}
					//if last line of file
					if(baseIndex == -1){
						break;
					}
					//number of credited hours for the employee
					float credit = 0;
					creditedHoursFile >> a>>a>>a>>a>>credit;
					//increase the credit for the right base
					initialCreditPerBase[baseIndex] += credit;
					//increase the total credit
					totalCredit += credit;

					//discard useless information
					creditedHoursFile.get();
					getline(creditedHoursFile,a);
					getline(creditedHoursFile,a);

				}
				//close creditedHours file
				creditedHoursFile.close();


			//remove the credit due to briefing/debriefing
				vector<int> briefingCredit;
				for(int i = 0 ; i<baseNames.size() ; i++){
					briefingCredit.push_back(0);
				}
				
				//calculate the number of briefing/debriefing credit in a previous solution
				calculateBriefingCredit(instanceNames[instanceNb],baseNames, &briefingCredit);

				//remove briefing credit for each base
				for(int i = 0;i<baseNames.size() ; i++){
					initialCreditPerBase[i] -= briefingCredit[i];
					totalCredit-= briefingCredit[i];
				}

			//find the base with the most credits
			//(this base will be the "main" base)
				int indexMax = 0;
				float maxCredit = 0;
				for(int i = 0 ; i<(int) initialCreditPerBase.size() ; i++){
					if(initialCreditPerBase[i] > maxCredit){
						maxCredit =initialCreditPerBase[i];
						indexMax = i;
					}
				}

				//add slack to total number of credit
				//total credit = total credit + slack
				totalCredit = totalCredit*(1.+percentSlack/100.);

				//--------------------------------------------
				//write the output file

				//open the file
				std::stringstream outname;
				outname << instanceNames[instanceNb] << "/credit_constrains.csv";
				std::ofstream output(outname.str().c_str(),ios::out | ios::trunc);

				//if unable to open output file, display an error message and proceed with next instance
				if(output.fail()){
					cout<<"Error : could not open output file credit_constrains in instance "<<instanceNames[instanceNb]<<". Proceeding with next instance."<<endl;
				}
				else{
					//write the % slack added
					output << "\" slack added : "<<percentSlack<<"%\""<<endl<<endl;

					/* write the number of credit for each base, accordig to different scenarios
					 * in one scenario, the base corresponding to "indexMax" gets a certain percentage
					 * (defined in the variable "percentages") of all credit, while the other bases split
					 * the rest evenly.
					 */

					//header
					
					output.width(6*baseNames.size());
					output.setf(std::ios::left);
					output <<"base";

					//write base names
					for(int i = 0 ; i< (int)baseNames.size();i++){
						output << " , ";
						output.width(10);
						output.setf(std::ios::left);
						output<<baseNames[i];
					}
					output<<endl;
					//end of header
					
					//writes the distribution of the credit
					
					
					//string for the percentage given to each base
					std::stringstream percentString;
					percentString.precision(3);

					//write the initial distribution if the parameter for the percentage to main base is set to -1 in param file
					if(percentage == -1){
						output.width(6*baseNames.size());
						output.setf(std::ios::left);
						
						//first, write a string containing the percentage of credit given to each base
						percentString << "\"";						
						for(int i = 0 ; i< baseNames.size() ; i++){
							percentString<<initialCreditPerBase[i]*(1.+percentSlack/100)/totalCredit*100<<"%";
						}
						percentString << "\"";
						output<<percentString.str();

						//write the credit for each base
						for(int j = 0 ; j< (int)baseNames.size() ; j++){
							output << " , ";
							output.width(10);
							output.setf(std::ios::left);

							output<<initialCreditPerBase[j]*(1.+percentSlack/100);

						}
						output<<endl;
					}
					else{

						//predefined percentage

						//string representing the distribution of the credit among the bases
						//  the syntax is x%/y%/z%...
						std::stringstream percentString;
						percentString.precision(3);
						for(int j = 0 ; j< (int)baseNames.size() ; j++){

							//if main base
							if(j == indexMax){
								percentString << percentage<<"%";
							}
							else{
								percentString << (100.-percentage)/(baseNames.size()-1)<<"%";
							}

							//if it is not the last percentage
							if(j != (int) baseNames.size()-1){
								percentString<<"/";
							}
						}

						output.width(6*baseNames.size());
						output.setf(std::ios::left);
						output << percentString.str();

						//credit allowed for the big base
						float bigCredit = percentage/100.*totalCredit;
						//credit allowed for the small base
						float smallCredit = (totalCredit-bigCredit)/(baseNames.size()-1);

						//write the credit for each base
						for(int j = 0 ; j< (int)baseNames.size() ; j++){
							output << " , ";
							output.width(10);
							output.setf(std::ios::left);
							if(j == indexMax){
								output << bigCredit;
							}
							else{
								output << smallCredit;
							}
						}
						output<<endl;
					}
					//close output file
					output.close();
				}
			}

			instanceNb++;
		}

	}
	return 0;
}

//find base names
bool baseName(string instanceName, vector<string> *baseNames){

	string a; //useless string used to burn characters
	string name;
	bool isbase;

	//name of file to open
	std::stringstream basesFileName;

	basesFileName <<instanceName<<"/listOfBases.csv";
	//opens the file containing the list of bases
	//open file
	std::ifstream bases(basesFileName.str().c_str(),ios::in);
	if(bases.fail()){
		return false;
	}

	getline(bases,a);//get rid of first line

	string line;
	//read name of airport and check if it is a base
	while(getline(bases,line)){
		//if line is not empty or only white spaces
		if( line.find_first_not_of(' ') != std::string::npos ){
			
			name = line.substr(0,line.find_first_of(" "));
			isbase = atoi(line.substr(line.find_first_of(",")+2,1).c_str());
			//if it is a base, add to list
			if(isbase){
				(*baseNames).push_back(name) ;
			}
		}
	}
	bases.close();
	return true;
}


//calculate the number of credit in briefings/debriefing
void calculateBriefingCredit(string instanceName,vector<string> baseNames, vector<int> *briefingCredit){
	//open "solution_0" file that contains the initial solution' schedule
	std::stringstream solutionFileName;// file name
	solutionFileName << instanceName<<"/solution_0";
	std::ifstream solutionFile(solutionFileName.str().c_str(),ios::in);

	//if unable to open "solution_0" file, display an error message and proceed with the next instance
	if(solutionFile.fail()){
		cout<<"Error : solution_0 file not found in instance folder "<<instanceName<<". Proceeding with next instance"<<endl;
	}
	else{



		/*The schedules are in the format :
		 *
		 * schedule 1 EMP007 (BASE3) : TASK--->TASK--->TASK--->...TASK;
		 * schedule 1 EMP003 (BASE1) : TASK--->TASK--->TASK--->...TASK;
		 * ...
		 *
		 * to determine the number of duties in each employee' schedule, we count the number of different days where airlegs (deadheads do not count) are assigned to the employee
		 *
		 * The day of an airleg is determined by the first numbers in the flight number.  for example, the airleg LEG_07_00960_0 is flown on the 7th day of the period
		 */

		//schedule of an employee
		string schedule;
		//base of an employee
		string baseString;

		//string used to discard useless input
		string a;
		//two first lines are useless
		getline(solutionFile,a);
		getline(solutionFile,a);

		//while there is a schedule left in the file
		//extract the base of the employee, surrounded by parentheses
		//extract the schedule and find the corresponding base in baseNames
		while( solutionFile >>a>>a>>a>>baseString>>a){
			//remove the parenthesis around the crew base string
			baseString = baseString.substr(1,baseString.length()-2);
			//get the schedule
			getline(solutionFile,schedule);
			//remove first space (white space)
			schedule = schedule.substr(1);

			//find base index
			int baseIndex = -1;
			for(int i = 0; i< (int) baseNames.size() ; i ++){
				//if base match
				if(baseString.compare(baseNames[i]) == 0){
					baseIndex = i;
					break;
				}
			}

			//if base is valid
			if(baseIndex != -1){

				/* while there are still tasks left, extract the next task from the current schedule and determine the action to take :
				 * if the task is either "VACATION", "POST_COURRIEL", "POST_PAIRING", or is a deadhead (starts by "TDH"), do nothing
				 * else, if the day of the airleg is greather than the day of the previous airleg, add 1 duty to the employee's base.
				 */

				//if there are tasks left in the schedule
				bool next = true;
				//position of a character in the schedule
				int pos;
				//current day of the schedule
				int day = 0;
				//name of the current task (vacation, LEG, etc.)
				string task;

				while(next){
					//find first occurence of "-", representing the end of a task
					pos = schedule.find('-');
					//if "-" character is found, remove the task from schedule and put it in the "task" variable
					if(pos !=  (int) string::npos){
						task = schedule.substr(0,pos);
						schedule = schedule.substr(pos+4);
					}
					//if "-" is not found, there is a last task
					else{
						//last task is not followed by "-"
						task = schedule.substr(0,schedule.length()-1);
						next = false;
					}
					//if task is an airleg (deadheads does not count)
					if(task.compare("VACATION") != 0 && task.substr(0,4).compare("POST")!=0 && task.substr(0,3).compare("TDH")!=0){
						//remove the prefix "PAL_" if it is a preferred airleg
						if(task.substr(0,3).compare("PAL") == 0){
							task = task.substr(4);
						}
						//extract the day of the flight from the flight number
						std::stringstream convert(task.substr(4,2));
						int taskDay;
						convert>>taskDay;
						//if new duty (new day), add 1 duty for the base and for the day
						if(taskDay > day){
							day = taskDay;
							//add 2h for the base (1h briefing, 1h debriefing) 
							(*briefingCredit)[baseIndex]+=1;
						}
					}
				}
			}
		}
	}
}

