//============================================================================
// Name        : crew_availability_constrains.cpp
// Author      : Frederic Quesnel
// Version     : 1.0
// Description : create crew availability constrains based on a previous solution.
//				 Two input files are created.  In "crew_avail_const", the crew availability
//				 is the same as in the previous solution.  In "crew_avail_const_avg", the crew
//				 availability correspond to the average over the period, for each base.
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
using std::setw;
using std::string;
using std::vector;


//selects base Names from file "listOfBases"
bool baseName(string, vector<string>*);

int main(int argc, char* argv[]) {

	//set seed of random number generator
	srand((unsigned)time(0));
	
	//------------------------------------------------
	//extracts arguments from params.txt

	string a;//dummy variable used to discard useless input
	
	std::stringstream paramsName(argv[1]);
	std::ifstream params(paramsName.str().c_str());
	string paramLine ;

	//list of paths to the instances
	vector<string> instanceNames;

	//skip lines until it arrives at the "instances" line
	while(getline(params,paramLine) && !paramLine.compare("instances")==0){
	}

	//extract instance names
	while(getline(params,paramLine) && !(paramLine.compare("")==0)){
		instanceNames.push_back(paramLine);
	}

	//skip lines until it arrives at the "crew_availability_constrains.cpp" line
	while(getline(params,paramLine) && !(paramLine.compare("crew_availability_constrains.cpp")==0)){
	}
	
	//extracts percentage of slack to be added from parameter file
	float percentSlack = 0;
	params >> a  >>a >>a >> percentSlack;

	//extracts percentage of crew availability to main base from parameter file
	float percentMainBase = 0;
	getline(params, a);
	params >> a >> a>>a>>percentMainBase;
	
	
	std::cout<<"parameters"<<std::endl;
	std::cout<<"percentSlack : "<<percentSlack<<std::endl;
	std::cout<<"percentMainBase : "<<percentMainBase<<std::endl;
	
	
	//number of current instance
	int instanceNb = 0;


	//----------------------------------------------------
	//generate crew availability constrains for each instance

	while(instanceNb < (int) instanceNames.size()){

		//base names
		vector<string> baseNames;

		//extracts base names from listOfBases.csv.  If the operation fails, display an error message and proceed with next instance.
		if(!baseName(instanceNames[instanceNb],&baseNames)){
			cout<<"Error : listOfBases.csv file not found in instance "<<instanceNames[instanceNb]<<endl;
		}
		else{


			//---------------------------------------
			//find the number of days in the period
			int numDays = 0;
			//condition to continue seeking for the next day
			bool nextDay = true;

			//while previous day files are found
			while(nextDay){

				//name of the next day file
				std::stringstream dayFileName;
				dayFileName<<instanceNames[instanceNb]<<"/day_"<<numDays+1<<".csv";
				//open the file
				std::ifstream dayFile(dayFileName.str().c_str());
	
				//if the programm fails to open the file, assume the period is over
				if(dayFile.fail()){
					break;
				}

				//close the stream
				dayFile.close();

				//if the next day file was opened, add 1 day to the period
				numDays++;
			}

			//-----------------------------------------
			//count the initial number of duties per base per day

			//number of duties per base per day in the initial solution
			vector< vector<int> > initialDutiesPerBasePerDay;

			//initialize initialDutiesPerBasePerDay for each base and for each day
			for(int i = 0 ; i < (int) baseNames.size() ; i++){
				//new vector
				initialDutiesPerBasePerDay.push_back( vector<int>() );

				for(int j = 0 ; j < numDays; j++){
					initialDutiesPerBasePerDay[i].push_back(0);
				}
			}

			//open "solution_0" file that contains the initial solution' schedule
			std::stringstream solutionFileName;// file name
			solutionFileName << instanceNames[instanceNb]<<"/solution_0";
			std::ifstream solutionFile(solutionFileName.str().c_str(),ios::in);

			//if unable to open "solution_0" file, display an error message and proceed with the next instance
			if(solutionFile.fail()){
				cout<<"Error : solution_0 file not found in instance folder "<<instanceNames[instanceNb]<<". Proceeding with next instance"<<endl;
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
									initialDutiesPerBasePerDay[baseIndex][taskDay-1]++;
								}
							}
						}
					}
				}

				//---------------------------------------------------------
				//first possible output : same objective as in the initial solution, with added slack
				//if the option for the percentage to main base is set to -1 in parameter file
								
				if(percentMainBase == -1){
					

					//Do the followint things:
					// - add slack for each base and each day
					// - round up or down each value so the sum stays the same, or as close as possible
					
					
					//declare and initialize variables used to compute which base to round up/down	
					vector<float> sumDutiesPerDay;
					vector<int> sumRoundedDownDutiesPerDay;
					for(int i = 0 ; i < numDays; i++){
						sumDutiesPerDay.push_back(0);
						sumRoundedDownDutiesPerDay.push_back(0);				
					}
					
					//duties per base per day with slack
					vector< vector<float> > dutiesPerBasePerDay;
					for(int i = 0 ; i < (int) baseNames.size() ; i++){
						//new vector
						dutiesPerBasePerDay.push_back( vector<float>() );
						for(int j = 0 ; j < numDays; j++){
							dutiesPerBasePerDay[i].push_back(0);
						}
					}
					
					//set all previously declared vector to their respective values
					//	- dutiesPerBasePerDay        : number of duties (with slack) for each base and each day
					//	- sumDutiesPerDay            : sum of duties (with slack) over the bases for each day
					//  - sumRoundedDownDutiesPerDay : sum of duties (with slack) over the bases for each day (rounded down before computing the sum)
					for(int i = 0 ; i < (int) baseNames.size() ; i++){
						for(int j = 0 ; j < numDays; j++){
							dutiesPerBasePerDay[i][j] = 1.*initialDutiesPerBasePerDay[i][j]* (1.+ percentSlack/100);
							sumDutiesPerDay[j] += dutiesPerBasePerDay[i][j];
							sumRoundedDownDutiesPerDay[j] += (int) dutiesPerBasePerDay[i][j];
						}
					}
					
					for(int j = 0 ; j < numDays; j++){
						//determine the number of bases to round up
						int numberRoundUp = 0;

						numberRoundUp = ((int) sumDutiesPerDay[j]+1)-sumRoundedDownDutiesPerDay[j];
						
						//at least 1 crew must be added. 
						if(numberRoundUp ==0){
						
							//if adding slack already increased the crew availability by at least 1 at one or more bases, 
							//we don't need to add one more
							bool addedOneCrew=false;
							for(int i = 0 ; i<baseNames.size(); i++){
								if(initialDutiesPerBasePerDay[i][j]< (int) dutiesPerBasePerDay[i][j]){
									addedOneCrew = true;
									break;
								}
							}
							if(!addedOneCrew){
								numberRoundUp =1;
							}
						}
						
						//round up the bases with the highest decimal point
						//if two or more bases have the same decimal point, add one randomly in one of these bases.
						for(int i = 0 ; i<numberRoundUp ; i++){
							//vector of the indexes of the bases having currently the highest decimal
							vector<int> indexHighestDecimal;
							//value of the shighest decmial found yet
							float highestDecimal = 0;
							for(int k = 0 ; k< baseNames.size(); k++){
								
								//if higher decimal is found : 
									//- clear list of highest decimal found
									//- add new base to list
								if(dutiesPerBasePerDay[k][j]- (int)dutiesPerBasePerDay[k][j] > highestDecimal){
									indexHighestDecimal.clear();
									indexHighestDecimal.push_back(k);
									highestDecimal = dutiesPerBasePerDay[k][j] - (int)dutiesPerBasePerDay[k][j];
								}
								//in case of a tie, just add the base to the list
								else if(dutiesPerBasePerDay[k][j]- (int)dutiesPerBasePerDay[k][j] - highestDecimal < 0.00001 
											&&
											dutiesPerBasePerDay[k][j]- (int)dutiesPerBasePerDay[k][j] - highestDecimal > -0.00001
								){
									indexHighestDecimal.push_back(k);
								}
								
					
							}
							//choose one base randomly among the bases having the highest decimal value
							int Chosen = rand() % indexHighestDecimal.size();
							dutiesPerBasePerDay[indexHighestDecimal[Chosen]][j] = (int)dutiesPerBasePerDay[indexHighestDecimal[Chosen]][j]+1;
						}
					}
						
					//compute the %slack actually added (not the same as wanted because of the rounding)
					
					//sum of duties after adding slack and rounding
					float sumDutiesPerBasePerDayFinal = 0;	
					//sum of duties before adding slack and rounding
					float sumDutiesPerBasePerDayInitial = 0;
					//percentage of slack actually added
					float percentSlackAdded=0;
					
					//calculate the sum of duties before/after adding slack and rounding
					for(int i = 0 ; i < (int) baseNames.size() ; i++){
						for(int j = 0 ; j < numDays; j++){
						
							sumDutiesPerBasePerDayFinal   += (int) dutiesPerBasePerDay[i][j];
							sumDutiesPerBasePerDayInitial += (int)initialDutiesPerBasePerDay[i][j];
							
						}
					}
					
					//compute slack added
					percentSlackAdded = (sumDutiesPerBasePerDayFinal/sumDutiesPerBasePerDayInitial - 1.)*100.	;
						
					//writes the output	
						
					//open the output files
					std::stringstream outputName;
					outputName << instanceNames[instanceNb]<<"/crew_avail_const.csv";
					std::ofstream output(outputName.str().c_str(),ios::out | ios::trunc);

					//if fails to open output, display an error mesage and proceed with next instance
					if(output.fail() || output.fail()){
						cout<<"Error : could not open first output file crew_avail_const.csv in instance "<<instanceNames[instanceNb]<<". Proceeding with next instance"<<endl;
					}
					else{

						//comment describing the file content
						output << "\"Number of crews available at each base for each day of the period.  " <<percentSlackAdded <<"\% slack added.\""<<endl<<endl;

						//header
						output.width(5);
						output.setf(std::ios::left);
						output << "base";

						//name of bases in header
						for(int i = 0 ; i < (int) baseNames.size(); i++){
							output <<" , ";
							output.width(5);
							output.setf(std::ios::left);
							output << baseNames[i];
						}
						output << endl;

						//each row is in the format :
						//for txt output:
						// Dayi   ,#Base1     ,#Base2     ,#Base3

						//for each day of the period, write the number of duties for each base
						for(int i = 0 ; i < numDays ; i++){
							output.width(5);
							//name of the day
							std::stringstream dayname;
							dayname <<"Day"<< i+1;
							output.setf(std::ios::left);
							output <<  dayname.str();
							//write the number of duties for each base
							for(int j = 0 ; j < (int) baseNames.size() ; j++){
								output <<" , ";
								output.width(5);
								output.setf(std::ios::left);
								output<< (int) dutiesPerBasePerDay[j][i];
							}
							output<<endl;
						}
						//close first output file
						output.close();
					}
				}
				else{
					
					//---------------------------------------------------------
					//second output : average over the whole month

					//compute the averages for each base and add slack
					
					//sum of the averages over the bases (with slack)
					float sumAverages = 0;
					//sum of the averages over the bases (without slack)
					float sumAveragesWithoutSlack=0;
					
					vector<float> averages;

					int mainBaseIndex = -1;
					int mainBaseAvailability = 0;
					for(int i = 0 ; i < (int) baseNames.size() ; i++){
						int sum = 0 ;
						for(int j = 0 ; j<numDays ; j++){
							sum += initialDutiesPerBasePerDay[i][j];
						}
						if(sum > mainBaseAvailability){
							mainBaseIndex = i;
							mainBaseAvailability = sum;
						}
						
						averages.push_back(1.*sum/numDays*(1.+percentSlack/100.));
						sumAverages+=1.*sum/numDays*(1.+percentSlack/100.);
						sumAveragesWithoutSlack+=1.*sum/numDays;					
					}

					//distribute the availability among the bases as specified in the parameter file
					for(int i = 0 ; i < (int) baseNames.size() ; i++){
						if(i == mainBaseIndex){
							averages[i] = sumAverages*percentMainBase/100.;
						}
						else{
							averages[i] = sumAverages*(1-percentMainBase/100)/(baseNames.size()-1);
						}
					}

					//round up the sum of the averages
					//if already an integer, add one as slack anyway
					int roundedSumAverages = sumAverages+1;
	
					/* now, the program decides which base averages will be rounded up or down.
					 * the sum of the averages (after rounding) must stay equal to roundedSumAverages
					 * The number of base averages that will be rounded up equals roundedSumAverages - sum( floor(averages) )
					 * we round the base averages that have the best decimal value
					 */

					//compute the number of base averages that will be rounded up
					int numberRoundedUp = roundedSumAverages;
					for(int i = 0 ; i< (int) averages.size() ; i++){
						numberRoundedUp -= (int) averages[i];
					}
	
					//round up the ones with highest decimal value
					for(int i = 0 ; i< numberRoundedUp ; i++){
						int index = 0;
						float decimal=0;
	
						for(int j = 0 ; j< (int) averages.size();j++){
							
							if(averages[j]- (int) averages[j] >= decimal){
								decimal = averages[j]- (int) averages[j];
								index = j;
							}
						}
						
						averages[index] = (int) averages[index] +1;
											
					}
					//round down everything (the ones that were rounded up stay the same)
					float sumAveragesAfterRounding = 0;
					for(int i = 0 ; i < (int) averages.size() ; i++){
						averages[i] = (int) averages[i];
						sumAveragesAfterRounding += averages[i];
					}
					float percentSlackAdded = (1.*sumAveragesAfterRounding/sumAveragesWithoutSlack-1)*100;
		
					//std::cout<<"total without slack : "<<sumAveragesWithoutSlack<<std::endl;
					//std::cout<<"total après arrondi : "<<sumAveragesAfterRounding<<std::endl;
					
					//open second output files
					std::stringstream outputNameAvg;
					outputNameAvg << instanceNames[instanceNb]<<"/crew_avail_const.csv";
					std::ofstream outputAvg(outputNameAvg.str().c_str(),ios::out | ios::trunc);
	

					//if fails to open output, display an error mesage and proceed with next instance
					if(outputAvg.fail()){
						cout<<"Error : could not open second output file crew_avail_const_avg.csv in instance "<<instanceNames[instanceNb]<<endl;
					}
					else{

						//comment describing the file content
						outputAvg << "\"Number of crews available at each base for each day of the period."<<percentSlackAdded<<"\% slack added.\""<<endl<<endl;
					
						//header
						outputAvg.width(5);
						outputAvg.setf(std::ios::left);
						outputAvg << "base";

						//name of bases in the header
						for(int i = 0 ; i < (int) baseNames.size(); i++){
							outputAvg <<" , ";
							outputAvg.width(5);
							outputAvg.setf(std::ios::left);
							outputAvg << baseNames[i];
						}
						outputAvg << endl;

						//each row is in the format :
						//for txt output:
						// Dayi   ,#Base1     ,#Base2     ,#Base3

						//for each day of the period, write the number of duties for each base
						for(int i = 0 ; i < numDays ; i++){
							outputAvg.width(5);
							std::stringstream dayname;
							dayname <<"Day"<< i+1;
							outputAvg.setf(std::ios::left);
							outputAvg <<  dayname.str();
							for(int j = 0 ; j < (int) baseNames.size() ; j++){
								outputAvg <<" , ";
								outputAvg.width(5);
								outputAvg.setf(std::ios::left);
								outputAvg<< averages[j];
							}
							outputAvg<<endl;
						}

						//close second output file
						outputAvg.close();
					}
				}
				solutionFile.close();
			}
			//next instance
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
